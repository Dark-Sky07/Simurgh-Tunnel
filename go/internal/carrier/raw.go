package carrier

import (
	"bufio"
	"crypto/ecdh"
	"crypto/hkdf"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"time"

	"log/slog"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/proto"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/third_party/xcrypto/chacha20poly1305"
)

// The `raw` carrier: no TLS, no certificate, no recognisable protocol.
//
// On the wire the first bytes are random (a random length noise prefix), then
// an X25519 public key, a timestamp and an HMAC tag -- after that
// ChaCha20-Poly1305 frames. To a filter this is "encrypted something"; to a
// prober it is noise: there is no banner, no reply to a wrong password, nothing
// to fingerprint except "somebody is speaking an unknown protocol here".
//
// This is byte for byte the format the Python engine speaks (simurgh/obfs.py),
// so the two engines can face each other.

const (
	rawPrefixMax   = 96
	rawHelloWait   = 15 * time.Second
	rawStampWindow = 300 // seconds a client stamp may be off
	rawReplyLen    = 48  // 32 byte public key + 16 byte tag
)

// ---------------------------------------------------------------------------
// the channel: [4 byte length][AEAD frame], like the Python AeadFrameChannel

var rawBuckets = []int{128, 256, 512, 1024, 2048, 4096, 8192, 16384}

type rawChannel struct {
	conn net.Conn
	rd   *bufio.Reader

	send cipher
	recv cipher

	sctr, rctr uint64
	pad        bool
	peer       string
	scratch    []byte
	out        []byte
}

// cipher is the little slice of cipher.AEAD this file needs (keeps the type
// obvious when reading the padding code).
type cipher interface {
	Seal(dst, nonce, plaintext, additionalData []byte) []byte
	Open(dst, nonce, ciphertext, additionalData []byte) ([]byte, error)
}

func rawNonce(ctr uint64) []byte {
	var n [12]byte
	binary.BigEndian.PutUint64(n[4:], ctr)
	return n[:]
}

// padTarget is the padded size of a frame, mirroring the Python side: the
// first bucket that fits, else the size rounded up to a multiple of 64.
func padTarget(need int) int {
	for _, b := range rawBuckets {
		if need <= b {
			return b
		}
	}
	return need + (64 - need%64)
}

// padded appends the random padding and the 2 byte length trailer. Without
// padding the frame goes out exactly as it is, trailer included, so the two
// engines agree on both shapes.
func (c *rawChannel) padded(frame []byte) []byte {
	need := len(frame) + 2
	target := need
	if c.pad {
		target = padTarget(need)
	}
	out := make([]byte, target)
	copy(out, frame)
	padlen := target - need
	if padlen > 0 {
		if _, err := rand.Read(out[len(frame) : len(frame)+padlen]); err != nil {
			padlen = 0
		}
	}
	binary.BigEndian.PutUint16(out[target-2:], uint16(padlen))
	return out
}

// write encrypts one plaintext body (an already padded frame) and puts it on
// the wire as [4 byte length][ciphertext] in a single write.
func (c *rawChannel) write(plain []byte) error {
	if cap(c.out) < 4+len(plain)+16 {
		c.out = make([]byte, 0, 4+len(plain)+16)
	}
	buf := c.out[:4]
	buf = c.send.Seal(buf, rawNonce(c.sctr), plain, nil)
	c.sctr++
	binary.BigEndian.PutUint32(buf[:4], uint32(len(buf)-4))
	_, err := c.conn.Write(buf)
	return err
}

// Name is what the logs, the panel and `simurgh status` show; it matches the
// Python engine's channel name for the same carrier.
func (c *rawChannel) Name() string { return "raw+aead" }
func (c *rawChannel) Peer() string { return c.peer }
func (c *rawChannel) Close() error { return c.conn.Close() }

// ReadFrame pulls the next frame: length, then one AEAD record, then the
// padding trailer.
func (c *rawChannel) ReadFrame() (byte, uint32, []byte, error) {
	var lenBuf [4]byte
	if _, err := io.ReadFull(c.rd, lenBuf[:]); err != nil {
		return 0, 0, nil, err
	}
	n := int(binary.BigEndian.Uint32(lenBuf[:]))
	// 16 bytes of tag + 2 bytes of trailer is the smallest possible record
	if n < 18 || n > proto.MaxFrame+128 {
		return 0, 0, nil, fmt.Errorf("raw: bad record length %d", n)
	}
	if cap(c.scratch) < n {
		c.scratch = make([]byte, n)
	}
	ct := c.scratch[:n]
	if _, err := io.ReadFull(c.rd, ct); err != nil {
		return 0, 0, nil, err
	}
	plain, err := c.recv.Open(ct[:0], rawNonce(c.rctr), ct, nil)
	if err != nil {
		return 0, 0, nil, errors.New("raw: frame authentication failed")
	}
	c.rctr++
	// two bytes of trailer is the shortest possible plaintext; the frame itself
	// may be as small as a control frame header
	if len(plain) < 2 {
		return 0, 0, nil, errors.New("raw: short frame")
	}
	padlen := int(binary.BigEndian.Uint16(plain[len(plain)-2:]))
	if padlen+2 > len(plain) {
		return 0, 0, nil, errors.New("raw: bad padding")
	}
	frame := plain[:len(plain)-2-padlen]
	if len(frame) < proto.HeaderLen {
		return 0, 0, nil, errors.New("raw: short frame body")
	}
	return frame[0], binary.BigEndian.Uint32(frame[1:proto.HeaderLen]), frame[proto.HeaderLen:], nil
}

// WriteFrame sends one complete frame.
func (c *rawChannel) WriteFrame(frame []byte) error {
	return c.write(c.padded(frame))
}

// WriteData sends a DATA frame without gluing the header and the payload
// together first: both go into the padded buffer in one copy.
func (c *rawChannel) WriteData(hdr, payload []byte) error {
	need := len(hdr) + len(payload) + 2
	target := need
	if c.pad {
		target = padTarget(need)
	}
	plain := make([]byte, target)
	copy(plain, hdr)
	copy(plain[len(hdr):], payload)
	padlen := target - need
	if padlen > 0 {
		if _, err := rand.Read(plain[target-padlen-2 : target-2]); err != nil {
			padlen = 0
		}
	}
	binary.BigEndian.PutUint16(plain[target-2:], uint16(padlen))
	return c.write(plain)
}

// ---------------------------------------------------------------------------
// handshake

// mac16 is the 16 byte tag both sides send in the handshake.
func mac16(key [32]byte, parts ...[]byte) []byte {
	h := hmac.New(sha256.New, key[:])
	for _, p := range parts {
		h.Write(p)
	}
	return h.Sum(nil)[:16]
}

// newAEAD builds the frame cipher; the key always comes from our own HKDF, so
// a wrong size here is a programming error, not a runtime condition.
func newAEAD(key []byte) cipher {
	aead, err := chacha20poly1305.New(key)
	if err != nil {
		panic("raw: " + err.Error())
	}
	return aead
}

// dialRaw runs the client half of the handshake.
func dialRaw(opt ClientOptions) (mux.Channel, error) {
	conn, err := dialTCP(opt.Address, opt.Port, opt.Timeout)
	if err != nil {
		return nil, err
	}
	curve := ecdh.X25519()
	priv, err := curve.GenerateKey(rand.Reader)
	if err != nil {
		conn.Close()
		return nil, err
	}
	pub := priv.PublicKey().Bytes()

	key := proto.AuthKey(opt.Token)
	ts := proto.ClientTimestamp()
	var tsBuf [8]byte
	binary.BigEndian.PutUint64(tsBuf[:], uint64(ts))

	var plenBuf [1]byte
	if _, err := rand.Read(plenBuf[:]); err != nil {
		conn.Close()
		return nil, err
	}
	// the byte on the wire is the prefix length itself, so it must stay within
	// what the server accepts (the Python engine does the same: urandom % 97)
	plen := int(plenBuf[0]) % (rawPrefixMax + 1)

	hello := make([]byte, 0, 1+plen+32+8+16)
	hello = append(hello, byte(plen))
	noise := make([]byte, plen)
	if _, err := rand.Read(noise); err != nil {
		conn.Close()
		return nil, err
	}
	hello = append(hello, noise...)
	hello = append(hello, pub...)
	hello = append(hello, tsBuf[:]...)
	hello = append(hello, mac16(key, []byte("simurgh/v2/raw"), tsBuf[:], pub)...)

	if _, err := conn.Write(hello); err != nil {
		conn.Close()
		return nil, err
	}

	_ = conn.SetReadDeadline(time.Now().Add(opt.Timeout))
	reply := make([]byte, rawReplyLen)
	if _, err := io.ReadFull(conn, reply); err != nil {
		conn.Close()
		return nil, err
	}
	spub, stag := reply[:32], reply[32:]
	expect := mac16(key, []byte("simurgh/v2/raw-s"), tsBuf[:], pub, spub)
	if !hmac.Equal(stag, expect) {
		conn.Close()
		return nil, errors.New("raw handshake failed (wrong token?)")
	}
	peerKey, err := curve.NewPublicKey(spub)
	if err != nil {
		conn.Close()
		return nil, errors.New("raw: bad server key")
	}
	shared, err := priv.ECDH(peerKey)
	if err != nil {
		conn.Close()
		return nil, errors.New("raw: bad shared secret")
	}
	salt := make([]byte, 0, 8+32+32)
	salt = append(salt, tsBuf[:]...)
	salt = append(salt, pub...)
	salt = append(salt, spub...)
	okm, err := hkdf.Key(sha256.New, shared, salt, "simurgh/v2/keys", 64)
	if err != nil {
		conn.Close()
		return nil, err
	}
	_ = conn.SetReadDeadline(time.Time{})
	ch := newRawChannel(conn, okm[:32], okm[32:], opt.Padding)
	slog.Debug("raw handshake done", "peer", conn.RemoteAddr().String())
	return ch, nil
}

func newRawChannel(conn net.Conn, sendKey, recvKey []byte, pad bool) *rawChannel {
	return &rawChannel{
		conn: conn,
		rd:   bufio.NewReaderSize(conn, readBuf),
		send: newAEAD(sendKey),
		recv: newAEAD(recvKey),
		pad:  pad,
		peer: conn.RemoteAddr().String(),
	}
}

// serveRaw runs the server half. Anything that does not authenticate is
// answered with noise and closed, exactly like the Python engine.
func (s *Server) serveRaw(conn net.Conn, onChannel func(mux.Channel)) {
	_ = conn.SetReadDeadline(time.Now().Add(rawHelloWait))
	br := bufio.NewReaderSize(conn, 4096)
	plen, err := br.ReadByte()
	if err != nil || int(plen) > rawPrefixMax {
		rawNoise(conn)
		return
	}
	rest := make([]byte, int(plen)+56)
	if _, err := io.ReadFull(br, rest); err != nil {
		conn.Close()
		return
	}
	cpub := rest[plen : plen+32]
	tsBytes := rest[plen+32 : plen+40]
	tag := rest[plen+40 : plen+56]

	key := proto.AuthKey(s.Token)
	first := make([]byte, 0, 8+32)
	first = append(first, tsBytes...)
	first = append(first, cpub...)
	if !hmac.Equal(tag, mac16(key, []byte("simurgh/v2/raw"), first)) {
		rawNoise(conn)
		return
	}
	ts := int64(binary.BigEndian.Uint64(tsBytes))
	if now := time.Now().Unix(); ts < now-rawStampWindow || ts > now+rawStampWindow {
		rawNoise(conn)
		return
	}

	curve := ecdh.X25519()
	priv, err := curve.GenerateKey(rand.Reader)
	if err != nil {
		conn.Close()
		return
	}
	spub := priv.PublicKey().Bytes()
	reply := make([]byte, 0, rawReplyLen)
	reply = append(reply, spub...)
	reply = append(reply, mac16(key, []byte("simurgh/v2/raw-s"), tsBytes, cpub, spub)...)
	if _, err := conn.Write(reply); err != nil {
		conn.Close()
		return
	}
	clientKey, err := curve.NewPublicKey(cpub)
	if err != nil {
		conn.Close()
		return
	}
	shared, err := priv.ECDH(clientKey)
	if err != nil {
		conn.Close()
		return
	}
	salt := make([]byte, 0, 8+32+32)
	salt = append(salt, tsBytes...)
	salt = append(salt, cpub...)
	salt = append(salt, spub...)
	okm, err := hkdf.Key(sha256.New, shared, salt, "simurgh/v2/keys", 64)
	if err != nil {
		conn.Close()
		return
	}
	_ = conn.SetReadDeadline(time.Time{})
	// the server sends with the second half of the key material
	ch := newRawChannel(conn, okm[32:], okm[:32], s.Padding)
	ch.rd = br // keep whatever the client already sent after its hello
	slog.Debug("raw client accepted", "peer", conn.RemoteAddr().String())
	onChannel(ch)
}

// rawNoise answers a prober with random bytes and hangs up: there is nothing
// here to learn, not even that the handshake failed.
func rawNoise(conn net.Conn) {
	buf := make([]byte, 64)
	if _, err := rand.Read(buf); err == nil {
		_, _ = conn.Write(buf)
	}
	time.Sleep(20 * time.Millisecond)
	conn.Close()
}
