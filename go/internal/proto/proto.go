// Package proto is the Simurgh v2 wire format: frame framing, the 25 byte
// authentication header and SOCKS style addresses.
//
// It is deliberately byte-for-byte compatible with the Python engine, so a Go
// relay can talk to a Python exit and the other way around.
package proto

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

const (
	// HeaderLen is the size of every frame header: 1 byte type + 4 byte sid.
	HeaderLen = 5
	// MaxFrame is the biggest frame either side will accept.
	MaxFrame = 16 << 20
)

// Frame types.
const (
	TOpen    byte = 1
	TOpenOK  byte = 2
	TOpenErr byte = 3
	TData    byte = 4
	TEof     byte = 5
	TClose   byte = 6
	TWin     byte = 7
	TPing    byte = 8
	TPong    byte = 9
	TCtrl    byte = 10
)

// Stream modes carried in the OPEN payload.
const (
	ModeTCP byte = 0
	ModeUDP byte = 1
)

// Error codes carried by OPEN_ERR.
const (
	ErrRefused     byte = 1
	ErrTimeout     byte = 2
	ErrUnreachable byte = 3
	ErrDenied      byte = 4
	ErrBadRequest  byte = 5
)

// Defaults shared by both roles.
const (
	DefaultStreamWindow = 256 << 10
	// DefaultMaxStreamWindow is the ceiling the window autotuner may reach for
	// one stream. Memory parked here follows demand (only a stream that really
	// drains fast grows), so the ceiling is the worst case per busy user: 4 MiB
	// still allows ~66 MB/s for one user across a 60 ms path, while a thousand
	// mostly idle users cost nothing. Raise it in the config for a single very
	// fat flow (see docs/CONFIGURATION.md).
	DefaultMaxStreamWindow = 8 << 20
	DefaultGlobalWindow    = 16 << 20
	DefaultChunk           = 64 << 10
	DefaultKeepalive       = 25 // seconds between PINGs
	CreditFlush            = 16 << 10
)

// ErrorText mirrors the Python table so a relay can show a useful message.
var ErrorText = map[byte]string{
	ErrRefused:     "connection refused",
	ErrTimeout:     "connection timed out",
	ErrUnreachable: "host unreachable",
	ErrDenied:      "denied by policy",
	ErrBadRequest:  "bad request",
}

// PutHeader writes type + sid into dst (at least HeaderLen bytes) and returns
// the header slice. The sid lives at offset 1, which the hot path relies on.
func PutHeader(dst []byte, typ byte, sid uint32) []byte {
	dst[0] = typ
	binary.BigEndian.PutUint32(dst[1:], sid)
	return dst[:HeaderLen]
}

// Sid extracts the stream id from a frame header.
func Sid(frame []byte) uint32 { return binary.BigEndian.Uint32(frame[1:HeaderLen]) }

// Frame builds a complete frame (header + payload) in one allocation.
func Frame(typ byte, sid uint32, payload []byte) []byte {
	out := make([]byte, HeaderLen+len(payload))
	PutHeader(out, typ, sid)
	copy(out[HeaderLen:], payload)
	return out
}

// WinPayload is the body of a WIN frame: per-stream grant then the
// connection-wide grant.
func WinPayload(grant, global uint32) []byte {
	out := make([]byte, 8)
	binary.BigEndian.PutUint32(out, grant)
	binary.BigEndian.PutUint32(out[4:], global)
	return out
}

// ErrFrame builds an OPEN_ERR frame.
func ErrFrame(sid uint32, code byte, text string) []byte {
	if text == "" {
		text = ErrorText[code]
	}
	return Frame(TOpenErr, sid, append([]byte{code}, text...))
}

// ParseErrFrame splits an OPEN_ERR payload into code and message.
func ParseErrFrame(payload []byte) (byte, string) {
	if len(payload) == 0 {
		return ErrRefused, ErrorText[ErrRefused]
	}
	return payload[0], string(payload[1:])
}

// ---------------------------------------------------------------------------
// authentication

// Version of the header format.
const Version = 1

// AuthWindow is the tolerated clock skew, in seconds.
const AuthWindow = 180

// StampSpread is how many distinct seconds one client may spread its stamps
// over. Two tunnels opened in the same second would otherwise carry identical
// headers and the server's replay cache would drop the second one.
const StampSpread = 120

var stampCounter atomic.Int64

// AuthKey derives the shared key from the token.
func AuthKey(token string) [32]byte {
	return sha256.Sum256([]byte("simurgh/v2/token|" + token))
}

// ClientTimestamp returns a timestamp that is unique per connection.
func ClientTimestamp() int64 {
	n := stampCounter.Add(1)
	return time.Now().Unix() + n%StampSpread
}

func mac16(key [32]byte, parts ...[]byte) []byte {
	h := hmac.New(sha256.New, key[:])
	for _, p := range parts {
		h.Write(p)
	}
	return h.Sum(nil)[:16]
}

// ClientHeader builds the 25 byte client header: VER + ts + HMAC[:16].
func ClientHeader(token string, ts int64) []byte {
	key := AuthKey(token)
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], uint64(ts))
	out := make([]byte, 0, 25)
	out = append(out, Version)
	out = append(out, b[:]...)
	out = append(out, mac16(key, []byte("simurgh/v2/c"), b[:])...)
	return out
}

// CheckClientHeader verifies a client header and returns its timestamp.
func CheckClientHeader(header []byte, token string) (int64, bool) {
	if len(header) < 25 || header[0] != Version {
		return 0, false
	}
	ts := int64(binary.BigEndian.Uint64(header[1:9]))
	key := AuthKey(token)
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], uint64(ts))
	if !hmac.Equal(mac16(key, []byte("simurgh/v2/c"), b[:]), header[9:25]) {
		return 0, false
	}
	skew := time.Since(time.Unix(ts, 0))
	if skew > AuthWindow*time.Second || skew < -AuthWindow*time.Second {
		return 0, false
	}
	return ts, true
}

// ServerHeader builds the server's answer, bound to the client timestamp.
func ServerHeader(token string, clientTS, ts int64) []byte {
	key := AuthKey(token)
	var cb, b [8]byte
	binary.BigEndian.PutUint64(cb[:], uint64(clientTS))
	binary.BigEndian.PutUint64(b[:], uint64(ts))
	out := make([]byte, 0, 25)
	out = append(out, Version)
	out = append(out, b[:]...)
	out = append(out, mac16(key, []byte("simurgh/v2/s"), cb[:], b[:])...)
	return out
}

// CheckServerHeader verifies the answer to our own header.
func CheckServerHeader(header []byte, token string, clientTS int64) bool {
	if len(header) < 25 || header[0] != Version {
		return false
	}
	key := AuthKey(token)
	var cb, b [8]byte
	binary.BigEndian.PutUint64(cb[:], uint64(clientTS))
	binary.BigEndian.PutUint64(b[:], uint64(binary.BigEndian.Uint64(header[1:9])))
	return hmac.Equal(mac16(key, []byte("simurgh/v2/s"), cb[:], b[:]), header[9:25])
}

// ReplayCache remembers recently seen authentication tags.
type ReplayCache struct {
	mu   sync.Mutex
	seen map[[16]byte]time.Time
	ttl  time.Duration
	size int
}

// NewReplayCache returns a cache with the same limits as the Python engine.
func NewReplayCache() *ReplayCache {
	return &ReplayCache{seen: make(map[[16]byte]time.Time), ttl: 300 * time.Second, size: 20000}
}

// Add records a tag; it returns false when the tag was seen before.
func (r *ReplayCache) Add(tag []byte) bool {
	if len(tag) < 16 {
		return false
	}
	var key [16]byte
	copy(key[:], tag[:16])
	now := time.Now()
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.seen) >= r.size {
		cutoff := now.Add(-r.ttl)
		for k, v := range r.seen {
			if v.After(cutoff) {
				continue
			}
			delete(r.seen, k)
		}
	}
	if _, dup := r.seen[key]; dup {
		return false
	}
	r.seen[key] = now.Add(r.ttl)
	return true
}

// ---------------------------------------------------------------------------
// addresses (SOCKS style, reused inside OPEN)

// Address types.
const (
	AtypV4     byte = 1
	AtypDomain byte = 3
	AtypV6     byte = 4
)

// EncodeAddr packs host:port the way OPEN carries it.
func EncodeAddr(host string, port int) ([]byte, error) {
	if port <= 0 || port > 65535 {
		return nil, fmt.Errorf("bad port: %d", port)
	}
	var out []byte
	if ip := net.ParseIP(host); ip != nil {
		if v4 := ip.To4(); v4 != nil {
			out = append([]byte{AtypV4}, v4...)
		} else {
			out = append([]byte{AtypV6}, ip.To16()...)
		}
	} else {
		raw := []byte(host)
		if len(raw) == 0 || len(raw) > 255 {
			return nil, fmt.Errorf("bad host: %q", host)
		}
		out = append([]byte{AtypDomain, byte(len(raw))}, raw...)
	}
	return append(out, byte(port>>8), byte(port)), nil
}

// DecodeAddr reads an address from the front of data and returns the rest.
func DecodeAddr(data []byte) (host string, port int, rest []byte, err error) {
	if len(data) < 2 {
		return "", 0, nil, errors.New("address too short")
	}
	atyp := data[0]
	var offset int
	switch atyp {
	case AtypV4:
		if len(data) < 7 {
			return "", 0, nil, errors.New("short ipv4 address")
		}
		host = net.IP(data[1:5]).String()
		offset = 5
	case AtypDomain:
		n := int(data[1])
		if len(data) < 4+n {
			return "", 0, nil, errors.New("short domain address")
		}
		host = string(data[2 : 2+n])
		offset = 2 + n
	case AtypV6:
		if len(data) < 19 {
			return "", 0, nil, errors.New("short ipv6 address")
		}
		host = net.IP(data[1:17]).String()
		offset = 17
	default:
		return "", 0, nil, fmt.Errorf("unknown atyp: %d", atyp)
	}
	if len(data) < offset+2 {
		return "", 0, nil, errors.New("short port")
	}
	port = int(binary.BigEndian.Uint16(data[offset : offset+2]))
	return host, port, data[offset+2:], nil
}

// AddrString is a small helper for logs.
func AddrString(host string, port int) string {
	return net.JoinHostPort(host, strconv.Itoa(port))
}
