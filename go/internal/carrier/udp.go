package carrier

// The "udp" carrier: a reliable, ordered byte stream over datagrams, wire
// compatible with simurgh/udpcarrier.py.
//
// Both engines speak exactly the same bytes, so a Python relay can talk to a
// Go exit and the other way round:
//
//	HELLO_HEAD = [type u8][cookie 8][ts u64][mac 16]                 (33 bytes)
//	  mac = HMAC-SHA256(key, [type][cookie][ts])[:16]
//	DATA_OUTER = [type u8][sid u32][ack u32][ackbits u32][rwnd u16]   (15 bytes)
//	DATA_INNER = [seq u32][fecK u8][fecR u8][fecI u8][length u16]     (9 bytes)
//	ACK_HEAD   = [type u8][sid u32][ack u32][bits u32][rwnd u16][echo u32]
//	CLOSE_HEAD = [type u8][sid u32]
//
// The AEAD is ChaCha20-Poly1305 with the packet id as the nonce and *only* the
// 9 inner bytes as the associated data: the outer header carries the ack fields,
// which change on a retransmit, so they must stay outside the tag.  Key =
// SHA-256("simurgh/v2/udp|" + token), and both sides derive the session id from
// the first four bytes of the client's cookie.

import (
	stdcipher "crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/proto"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/third_party/xcrypto/chacha20poly1305"
)

func slogDebug(msg string, args ...any) {
	slog.Debug("udp "+msg, args...)
}

const (
	udpHelloC = 1
	udpHelloS = 2
	udpData   = 3
	udpAck    = 4
	udpClose  = 5

	udpHelloSize     = 33
	udpDataOuterSize = 15
	udpDataInnerSize = 9
	udpAckSize       = 19
	udpCloseSize     = 5

	udpAEADTag    = 16
	udpMinMTU     = 512
	udpMaxMTU     = 60000
	udpDefaultMTU = 1200

	udpSackBits    = 32
	udpMaxRetries  = 14
	udpParityBase  = 0x80000000
	udpMaxGroups   = 8
	udpMaxBuffered = 8 << 20
	udpBurstBytes  = 128 << 10
	udpSockBuffer  = 4 << 20
	// A queued datagram costs the kernel more than the payload you can see in
	// it (measured at ~1.9x for ~1.2 KiB packets), so the window we advertise
	// is a fraction of SO_RCVBUF, never the whole thing.
	udpSockRatio = 3.0 / 8.0

	udpTick        = 5 * time.Millisecond
	udpIdleTimeout = 120 * time.Second
	udpHelloWindow = 180 * time.Second

	udpRTOMin     = 30 * time.Millisecond
	udpRTOMax     = 2 * time.Second
	udpRTOInitial = 250 * time.Millisecond
)

// UdpCarrierError reports a datagram carrier that cannot run.
type UdpCarrierError struct{ msg string }

func (e *UdpCarrierError) Error() string { return e.msg }

func udpAuthKey(token string) []byte {
	sum := sha256.Sum256([]byte("simurgh/v2/udp|" + token))
	return sum[:]
}

func udpHelloMAC(key []byte, kind byte, cookie []byte, ts uint64) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte{kind})
	mac.Write(cookie)
	var buf [8]byte
	binary.BigEndian.PutUint64(buf[:], ts)
	mac.Write(buf[:])
	return mac.Sum(nil)[:16]
}

func udpHelloPacket(key []byte, kind byte, cookie []byte, ts uint64) []byte {
	out := make([]byte, udpHelloSize)
	out[0] = kind
	copy(out[1:9], cookie)
	binary.BigEndian.PutUint64(out[9:17], ts)
	copy(out[17:33], udpHelloMAC(key, kind, cookie, ts))
	return out
}

func udpHelloFields(data []byte) (byte, []byte, uint64, []byte) {
	return data[0], data[1:9], binary.BigEndian.Uint64(data[9:17]), data[17:33]
}

func udpMSS(mtu int) int {
	mss := mtu - udpDataOuterSize - udpDataInnerSize - udpAEADTag
	if mss < 200 {
		return 200
	}
	return mss
}

func udpNonce(id uint32) []byte {
	var out [12]byte
	binary.BigEndian.PutUint32(out[8:], id)
	return out[:]
}

// ParseFEC turns "8:2" (or "off") into a (k, r) pair, matching the Python side.
func ParseFEC(value string) (int, int) {
	text := strings.TrimSpace(strings.ToLower(value))
	if text == "" || text == "off" || text == "none" || text == "0" {
		return 0, 0
	}
	parts := strings.SplitN(strings.ReplaceAll(text, "/", ":"), ":", 2)
	if len(parts) != 2 {
		return 0, 0
	}
	var k, r int
	if _, err := fmt.Sscanf(strings.TrimSpace(parts[0]), "%d", &k); err != nil {
		return 0, 0
	}
	if _, err := fmt.Sscanf(strings.TrimSpace(parts[1]), "%d", &r); err != nil {
		return 0, 0
	}
	if k < 2 || k > 250 || r < 1 || r > 250 {
		return 0, 0
	}
	return k, r
}

func clampMTU(mtu int) int {
	if mtu < udpMinMTU {
		return udpMinMTU
	}
	if mtu > udpMaxMTU {
		return udpMaxMTU
	}
	return mtu
}

// ---------------------------------------------------------------------------
// the reliable stream

type udpSegment struct {
	seq       uint32
	size      int
	cipher    []byte
	packet    []byte // the exact bytes of the first send
	sent      time.Time
	firstSent time.Time
	retries   int
}

// udpStream is a reliable, ordered byte stream over one datagram session.
type udpStream struct {
	core *udpSession
	mss  int

	mu       sync.Mutex
	cond     *sync.Cond // signalled when the receive buffer grows
	writable *sync.Cond // signalled when the send queue drains

	recv     []byte
	rcvNxt   uint32
	ooo      map[uint32][]byte
	oooBytes int

	sendq    map[uint32]*udpSegment
	unsent   []uint32
	inFlight int
	nextSeq  uint32

	peerWindow int
	cwnd       int
	ssthresh   int
	rtt        time.Duration
	rttvar     time.Duration
	rto        time.Duration

	pendingAck int
	lastAckAt  time.Time
	parity     [][]byte

	sentSegments int64
	retransmits  int64
	paritySent   int64
	recovered    int64
	bytesIn      int64
	bytesOut     int64
	dupPackets   int64
	sendBlocks   int64
	dupAcks      int64

	closed bool
	err    error
}

func newUDPStream(core *udpSession) *udpStream {
	s := &udpStream{
		core:       core,
		mss:        core.mss,
		rcvNxt:     0,
		ooo:        map[uint32][]byte{},
		sendq:      map[uint32]*udpSegment{},
		peerWindow: 1024 * 1200,
		cwnd:       16 << 10,
		ssthresh:   128 << 10,
		rtt:        udpRTOInitial,
		rttvar:     udpRTOInitial / 2,
		rto:        udpRTOInitial,
	}
	s.cond = sync.NewCond(&s.mu)
	s.writable = sync.NewCond(&s.mu)
	return s
}

// ------------------------------------------------------------------ receiving

func (s *udpStream) deliver(seq uint32, data []byte) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if seq < s.rcvNxt {
		s.dupPackets++
		return false
	}
	s.bytesIn += int64(len(data))
	if seq == s.rcvNxt {
		s.recv = append(s.recv, data...)
		s.rcvNxt++
		for {
			chunk, ok := s.ooo[s.rcvNxt]
			if !ok {
				break
			}
			delete(s.ooo, s.rcvNxt)
			s.oooBytes -= len(chunk)
			s.recv = append(s.recv, chunk...)
			s.rcvNxt++
		}
		s.cond.Broadcast()
	} else if seq-s.rcvNxt < 1<<16 {
		if _, have := s.ooo[seq]; !have {
			if s.oooBytes+len(data) > udpMaxBuffered {
				return false
			}
			clone := append([]byte(nil), data...)
			s.ooo[seq] = clone
			s.oooBytes += len(clone)
		}
	}
	return true
}

// readFrame returns the next framed payload (4 byte big-endian length first),
// blocking until it arrives.
func (s *udpStream) readFrame() ([]byte, error) {
	for {
		s.mu.Lock()
		if len(s.recv) >= 4 {
			size := int(binary.BigEndian.Uint32(s.recv[:4]))
			if size < 0 || size > 64<<20 {
				s.mu.Unlock()
				return nil, fmt.Errorf("frame too large: %d", size)
			}
			if len(s.recv) >= 4+size {
				out := make([]byte, size)
				copy(out, s.recv[4:4+size])
				s.recv = s.recv[4+size:]
				s.mu.Unlock()
				return out, nil
			}
		}
		if s.closed {
			err := s.err
			s.mu.Unlock()
			if err == nil {
				err = errors.New("udp stream closed")
			}
			return nil, err
		}
		s.cond.Wait()
		s.mu.Unlock()
	}
}

func (s *udpStream) recvWindowPackets() int {
	used := len(s.recv) + s.oooBytes
	free := udpMaxBuffered - used
	if cap := s.core.rwndBytes; cap < free {
		free = cap
	}
	if free <= 0 {
		return 1
	}
	n := free / s.mss
	if n < 1 {
		n = 1
	}
	if n > 65535 {
		n = 65535
	}
	return n
}

func (s *udpStream) sackBitsLocked() uint32 {
	var bits uint32
	for seq := range s.ooo {
		delta := seq - s.rcvNxt
		if delta > 0 && delta < udpSackBits {
			bits |= 1 << delta
		}
	}
	return bits
}

const (
	ackEager = 2 // pending acks that force a packet out
	ackGrace = 20 * time.Millisecond
	ackIdle  = 200 * time.Millisecond
)

func (s *udpStream) queueAck(force bool) {
	s.mu.Lock()
	s.pendingAck++
	now := time.Now()
	if !force && s.pendingAck < ackEager && now.Sub(s.lastAckAt) < ackGrace {
		s.mu.Unlock()
		return
	}
	rcvNxt := s.rcvNxt
	bits := s.sackBitsLocked()
	rwnd := uint16(s.recvWindowPackets())
	s.pendingAck = 0
	s.lastAckAt = now
	s.mu.Unlock()
	s.sendAck(rcvNxt, bits, rwnd)
}

func (s *udpStream) sendAck(rcvNxt, bits uint32, rwnd uint16) {
	s.core.sendAck(rcvNxt, bits, rwnd)
}

// -------------------------------------------------------------------- sending

// write queues payload bytes as segments and returns without waiting for them
// to reach the wire (the sender goroutine does that).
func (s *udpStream) write(payload []byte) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	view := payload
	for len(view) > 0 {
		n := s.mss
		if len(view) < n {
			n = len(view)
		}
		piece := append([]byte(nil), view[:n]...)
		seq := s.nextSeq
		s.nextSeq++
		s.sendq[seq] = &udpSegment{seq: seq, size: len(piece)}
		s.core.payloads[seq] = piece
		s.unsent = append(s.unsent, seq)
		s.bytesOut += int64(len(piece))
		view = view[n:]
	}
	s.mu.Unlock()
	s.transmit()
}

// writeFramed prepends the 4 byte length prefix the mux expects.
func (s *udpStream) writeFramed(frame []byte) {
	buf := make([]byte, 4+len(frame))
	binary.BigEndian.PutUint32(buf[:4], uint32(len(frame)))
	copy(buf[4:], frame)
	s.write(buf)
}

func (s *udpStream) transmit() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.transmitLocked()
}

func (s *udpStream) transmitLocked() {
	if s.closed {
		return
	}
	now := time.Now()
	budget := udpBurstBytes
	for len(s.unsent) > 0 {
		seq := s.unsent[0]
		segment, ok := s.sendq[seq]
		if !ok || !segment.sent.IsZero() {
			s.unsent = s.unsent[1:]
			continue
		}
		if s.inFlight+segment.size > s.windowLocked() || budget <= 0 {
			break
		}
		if !s.sendSegmentLocked(segment, now) {
			break
		}
		s.unsent = s.unsent[1:]
		budget -= segment.size + 48
	}
	for len(s.parity) > 0 && budget > 0 {
		packet := s.parity[0]
		if s.inFlight+len(packet) > s.windowLocked()*5/4 {
			break
		}
		if !s.core.send(packet) {
			break
		}
		s.parity = s.parity[1:]
		budget -= len(packet) + 48
	}
	s.writable.Broadcast()
}

func (s *udpStream) windowLocked() int {
	w := s.cwnd
	if s.peerWindow < w {
		w = s.peerWindow
	}
	if w < s.mss {
		return s.mss
	}
	return w
}

func (s *udpStream) sendSegmentLocked(segment *udpSegment, now time.Time) bool {
	payload, ok := s.core.payloads[segment.seq]
	if !ok {
		return false
	}
	first := segment.packet == nil
	if first {
		packet, cipher := s.core.packEncrypted(segment.seq, payload)
		segment.packet = packet
		segment.cipher = cipher
	} else if segment.cipher == nil {
		return false
	}
	if !s.core.send(segment.packet) {
		// the socket queue was full: the packet is not on the wire, so it must
		// not count as sent (or as a retry)
		s.sendBlocks++
		return false
	}
	if first {
		s.inFlight += segment.size
		segment.firstSent = now
		s.core.notePart(segment.seq, segment.cipher, s)
	}
	segment.sent = now
	s.sentSegments++
	if segment.retries > 0 {
		s.retransmits++
	}
	return true
}

// --------------------------------------------------------------------- acks

func (s *udpStream) ack(ack uint32, bits uint32, rwnd uint16) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if int(rwnd)*s.mss > s.mss {
		s.peerWindow = int(rwnd) * s.mss
	}
	now := time.Now()
	acked := 0
	sackedAhead := false

	// the cumulative part is a prefix of the ordered send queue
	for len(s.sendq) > 0 {
		var lowest uint32
		lowest = ^uint32(0)
		for seq := range s.sendq {
			if seq < lowest {
				lowest = seq
			}
		}
		if lowest >= ack {
			break
		}
		s.releaseLocked(s.sendq[lowest], now)
		delete(s.sendq, lowest)
		acked++
	}
	if bits != 0 {
		for delta := 1; delta < udpSackBits; delta++ {
			if bits&(1<<uint(delta)) == 0 {
				continue
			}
			segment, ok := s.sendq[ack+uint32(delta)]
			if !ok {
				continue
			}
			s.releaseLocked(segment, now)
			delete(s.sendq, ack+uint32(delta))
			acked++
			sackedAhead = true
		}
	}
	if sackedAhead && len(s.sendq) > 0 {
		var gap uint32 = ^uint32(0)
		for seq := range s.sendq {
			if seq < gap {
				gap = seq
			}
		}
		if gap >= ack {
			s.fastRetransmitLocked(gap, now)
		}
	}
	if acked > 0 && s.inFlight < s.windowLocked() {
		s.growWindowLocked()
	}
	s.transmitLocked()
}

func (s *udpStream) releaseLocked(segment *udpSegment, now time.Time) {
	delete(s.core.payloads, segment.seq)
	if !segment.firstSent.IsZero() && segment.retries == 0 {
		// Karn: a retransmitted packet has an ambiguous trip time
		s.updateRTTLocked(now.Sub(segment.firstSent))
	}
	s.inFlight -= segment.size
	if s.inFlight < 0 {
		s.inFlight = 0
	}
}

func (s *udpStream) growWindowLocked() {
	if s.cwnd < s.ssthresh {
		s.cwnd += s.mss
		if s.cwnd > s.ssthresh {
			s.cwnd = s.ssthresh
		}
		return
	}
	inc := s.mss * s.mss / maxInt(s.cwnd, 1)
	if inc < 1 {
		inc = 1
	}
	s.cwnd += inc
}

func (s *udpStream) fastRetransmitLocked(seq uint32, now time.Time) {
	segment, ok := s.sendq[seq]
	if !ok || segment.sent.IsZero() {
		return
	}
	// too fresh: probably reordering, not loss
	floor := s.rtt / 2
	if floor < 2*time.Millisecond {
		floor = 2 * time.Millisecond
	}
	if now.Sub(segment.sent) < floor {
		return
	}
	segment.retries++
	if segment.retries == 1 {
		s.ssthresh = maxInt(8*s.mss, s.cwnd/2)
		s.cwnd = maxInt(2*s.mss, s.cwnd/2)
	}
	s.sendSegmentLocked(segment, now)
}

func (s *udpStream) updateRTTLocked(sample time.Duration) {
	if sample <= 0 || sample > 10*time.Second {
		return
	}
	diff := s.rtt - sample
	if diff < 0 {
		diff = -diff
	}
	s.rttvar = (3*s.rttvar + diff) / 4
	s.rtt = (7*s.rtt + sample) / 8
	rto := s.rtt + 4*s.rttvar
	if rto < udpRTOMin {
		rto = udpRTOMin
	}
	if rto > udpRTOMax {
		rto = udpRTOMax
	}
	s.rto = rto
}

func (s *udpStream) onTimeoutLocked(now time.Time) {
	for _, segment := range s.sendq {
		if segment.sent.IsZero() || now.Sub(segment.sent) < s.rto {
			continue
		}
		if segment.retries >= udpMaxRetries {
			s.failLocked(fmt.Errorf("the udp tunnel stopped answering (no acks for %s)",
				time.Duration(udpMaxRetries)*s.rto))
			return
		}
		segment.retries++
		if segment.retries == 1 {
			// one halving per loss event, not a collapse to a single packet
			s.ssthresh = maxInt(8*s.mss, s.cwnd/2)
			s.cwnd = maxInt(2*s.mss, s.cwnd/2)
		}
		s.sendSegmentLocked(segment, now)
		s.core.rtoEvents.Add(1)
		return
	}
}

func (s *udpStream) failLocked(err error) {
	if s.closed {
		return
	}
	s.closed = true
	s.err = err
	s.cond.Broadcast()
	s.writable.Broadcast()
}

// ------------------------------------------------------------------ FEC

func (s *udpStream) trackGroup(seq uint32, cipher []byte, k int) {
	if k == 0 {
		return
	}
	group := seq / uint32(k)
	bucket := s.core.recvGroups[group]
	if bucket == nil {
		bucket = map[uint32][]byte{}
		s.core.recvGroups[group] = bucket
	}
	bucket[seq] = cipher
	if len(bucket) >= k {
		delete(s.core.recvGroups, group)
	}
	for len(s.core.recvGroups) > udpMaxGroups {
		s.core.dropOldestGroup()
	}
}

// recover rebuilds one missing member of a parity group.
func (s *udpStream) recover(packetID uint32, k, r, parityIndex int, body []byte) bool {
	if s.core.fecK == 0 || k != s.core.fecK || r == 0 || len(body) == 0 {
		return false
	}
	group := (packetID & 0x7FFFFFFF) >> 8
	base := group * uint32(k)
	parts := s.core.recvGroups[group]

	s.mu.Lock()
	rcvNxt := s.rcvNxt
	s.mu.Unlock()

	missing := -1
	for i := 0; i < k; i++ {
		if i%r != parityIndex {
			continue
		}
		seq := base + uint32(i)
		if _, ok := parts[seq]; !ok && seq >= rcvNxt {
			if missing >= 0 {
				return false // more than one member missing: parity cannot help
			}
			missing = i
		}
	}
	if missing < 0 {
		return false
	}
	acc := append([]byte(nil), body...)
	for i := 0; i < k; i++ {
		if i%r != parityIndex || i == missing {
			continue
		}
		chunk, ok := parts[base+uint32(i)]
		if !ok || len(chunk) != len(acc) {
			return false
		}
		for pos := range acc {
			acc[pos] ^= chunk[pos]
		}
	}
	seq := base + uint32(missing)
	head := s.core.inner(seq, len(acc), k, 0, 0)
	plain, err := s.core.decrypt(seq, acc, head)
	if err != nil {
		return false
	}
	s.core.recovered.Add(1)
	s.trackGroup(seq, acc, k)
	fresh := s.deliver(seq, plain)
	s.queueAck(!fresh)
	return fresh
}

// ------------------------------------------------------------------ helpers

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// ---------------------------------------------------------------------------
// the session

type udpSession struct {
	mu       sync.Mutex
	stream   *udpStream
	sendFn   func([]byte) bool
	peerAddr net.Addr

	tokenValue string
	tokenName  string
	key        []byte
	aead       stdcipher.AEAD
	sid        uint32
	cookie     []byte
	mtu        int
	mss        int
	fecK       int
	fecR       int
	window     int
	rwndBytes  int
	isServer   bool

	payloads   map[uint32][]byte
	recvGroups map[uint32]map[uint32][]byte
	sendGroups map[uint32]map[int][]byte

	established chan struct{}
	estOnce     sync.Once
	closed      chan struct{}
	closeOnce   sync.Once
	closeErr    error

	lastRx     time.Time
	rtoEvents  atomic.Int64
	recovered  atomic.Int64
	badPackets atomic.Int64
	parityRecv atomic.Int64
}

func newUDPSession(token string, mtu int, fecK, fecR, window int,
	sendFn func([]byte) bool, isServer bool) *udpSession {
	sess := &udpSession{
		sendFn:      sendFn,
		tokenValue:  token,
		mtu:         clampMTU(mtu),
		window:      window,
		fecK:        fecK,
		fecR:        fecR,
		isServer:    isServer,
		payloads:    map[uint32][]byte{},
		recvGroups:  map[uint32]map[uint32][]byte{},
		sendGroups:  map[uint32]map[int][]byte{},
		established: make(chan struct{}),
		closed:      make(chan struct{}),
		lastRx:      time.Now(),
	}
	sess.mss = udpMSS(sess.mtu)
	if sess.window < 32 {
		sess.window = 32
	}
	sess.rwndBytes = sess.window * sess.mss
	if sess.rwndBytes > int(udpSockBuffer*udpSockRatio) {
		sess.rwndBytes = int(udpSockBuffer * udpSockRatio)
	}
	cookie := make([]byte, 8)
	_, _ = rand.Read(cookie)
	sess.cookie = cookie
	sess.stream = newUDPStream(sess)
	return sess
}

func (s *udpSession) noteSocketCapacity(capacity int) {
	if capacity <= 0 {
		return
	}
	want := int(float64(capacity) * udpSockRatio)
	if want < 32<<10 {
		want = 32 << 10
	}
	limit := s.window * s.mss
	if want > limit {
		want = limit
	}
	s.rwndBytes = want
}

func (s *udpSession) send(data []byte) bool {
	select {
	case <-s.closed:
		return false
	default:
	}
	if s.sendFn == nil {
		return false
	}
	return s.sendFn(data)
}

func (s *udpSession) fail(err error) {
	s.closeOnce.Do(func() {
		s.closeErr = err
		close(s.closed)
		s.stream.mu.Lock()
		s.stream.failLocked(err)
		s.stream.mu.Unlock()
	})
}

func (s *udpSession) err() error {
	select {
	case <-s.closed:
		return s.closeErr
	default:
		return nil
	}
}

// ------------------------------------------------------------------ crypto

func (s *udpSession) initAEAD(key []byte) error {
	aead, err := chacha20poly1305.New(key)
	if err != nil {
		return err
	}
	s.key = key
	s.aead = aead
	return nil
}

// outer builds the cleartext part of a DATA packet.  Every caller already holds
// the stream lock (the ack fields come straight out of it), so it must not take
// it again: a Go mutex is not reentrant.
func (s *udpSession) outer() []byte {
	out := make([]byte, udpDataOuterSize)
	out[0] = udpData
	binary.BigEndian.PutUint32(out[1:5], s.sid)
	st := s.stream
	rcvNxt := st.rcvNxt
	bits := st.sackBitsLocked()
	rwnd := uint16(st.recvWindowPackets())
	binary.BigEndian.PutUint32(out[5:9], rcvNxt)
	binary.BigEndian.PutUint32(out[9:13], bits)
	binary.BigEndian.PutUint16(out[13:15], rwnd)
	return out
}

func (s *udpSession) inner(seq uint32, length, fecK, fecR, fecI int) []byte {
	out := make([]byte, udpDataInnerSize)
	binary.BigEndian.PutUint32(out[0:4], seq)
	out[4] = byte(fecK & 0xFF)
	out[5] = byte(fecR & 0xFF)
	out[6] = byte(fecI & 0xFF)
	binary.BigEndian.PutUint16(out[7:9], uint16(length&0xFFFF))
	return out
}

func (s *udpSession) packEncrypted(seq uint32, payload []byte) ([]byte, []byte) {
	head := s.inner(seq, len(payload)+udpAEADTag, s.fecK, 0, 0)
	cipher := s.aead.Seal(nil, udpNonce(seq), payload, head)
	out := make([]byte, 0, udpDataOuterSize+udpDataInnerSize+len(cipher))
	out = append(out, s.outer()...)
	out = append(out, head...)
	out = append(out, cipher...)
	return out, cipher
}

func (s *udpSession) decrypt(seq uint32, body, head []byte) ([]byte, error) {
	if s.aead == nil {
		return nil, errors.New("udp session is not authenticated yet")
	}
	return s.aead.Open(nil, udpNonce(seq), body, head)
}

// ------------------------------------------------------------------ handshake

func (s *udpSession) clientHello() []byte {
	return udpHelloPacket(udpAuthKey(s.tokenValue), udpHelloC, s.cookie,
		uint64(time.Now().Unix()))
}

// acceptHello authenticates a client hello; a stranger hears nothing back.
func (s *udpSession) acceptHello(data []byte, tokens [][]byte, names []string) bool {
	if len(data) < udpHelloSize || data[0] != udpHelloC {
		return false
	}
	_, cookie, ts, mac := udpHelloFields(data)
	if diff := time.Since(time.Unix(int64(ts), 0)); diff > udpHelloWindow || diff < -udpHelloWindow {
		return false
	}
	for i, key := range tokens {
		if hmac.Equal(mac, udpHelloMAC(key, udpHelloC, cookie, ts)) {
			if err := s.initAEAD(key); err != nil {
				return false
			}
			if i < len(names) {
				s.tokenName = names[i]
			}
			s.cookie = append([]byte(nil), cookie...)
			s.sid = binary.BigEndian.Uint32(cookie[:4])
			if s.sid == 0 {
				s.sid = 1
			}
			s.send(udpHelloPacket(key, udpHelloS, cookie, uint64(time.Now().Unix())))
			s.markEstablished()
			return true
		}
	}
	return false
}

func (s *udpSession) acceptReply(data []byte, key []byte) bool {
	if len(data) < udpHelloSize || data[0] != udpHelloS {
		return false
	}
	_, cookie, ts, mac := udpHelloFields(data)
	if !hmac.Equal(cookie, s.cookie) {
		return false
	}
	if diff := time.Since(time.Unix(int64(ts), 0)); diff > udpHelloWindow || diff < -udpHelloWindow {
		return false
	}
	if !hmac.Equal(mac, udpHelloMAC(key, udpHelloS, cookie, ts)) {
		return false
	}
	if err := s.initAEAD(key); err != nil {
		return false
	}
	s.sid = binary.BigEndian.Uint32(cookie[:4])
	if s.sid == 0 {
		s.sid = 1
	}
	s.markEstablished()
	return true
}

func (s *udpSession) markEstablished() {
	s.estOnce.Do(func() { close(s.established) })
}

func (s *udpSession) isEstablished() bool {
	select {
	case <-s.established:
		return true
	default:
		return false
	}
}

// ------------------------------------------------------------------ packets

// deliverPacket handles one DATA packet; it returns whether new bytes arrived.
func (s *udpSession) deliverPacket(data []byte) bool {
	if len(data) < udpDataOuterSize+udpDataInnerSize || s.aead == nil {
		return false
	}
	sid := binary.BigEndian.Uint32(data[1:5])
	if sid != 0 && s.sid != 0 && sid != s.sid {
		return false
	}
	if ack := binary.BigEndian.Uint32(data[5:9]); ack != 0 {
		bits := binary.BigEndian.Uint32(data[9:13])
		rwnd := binary.BigEndian.Uint16(data[13:15])
		s.stream.ack(ack, bits, rwnd)
	}
	pos := udpDataOuterSize
	seq := binary.BigEndian.Uint32(data[pos : pos+4])
	fecK := int(data[pos+4])
	fecR := int(data[pos+5])
	fecI := int(data[pos+6])
	length := int(binary.BigEndian.Uint16(data[pos+7 : pos+9]))
	if fecR != 0 {
		s.parityRecv.Add(1)
		// the packet id travels in the sequence field: 0x80000000 | group<<8 | i
		packetID := seq
		body := data[pos+udpDataInnerSize:]
		if length > 0 && len(body) >= length {
			body = body[:length]
		}
		return s.stream.recover(packetID, fecK, fecR, fecI, body)
	}
	if length == 0 || len(data) < pos+udpDataInnerSize+length {
		return false
	}
	head := data[pos : pos+udpDataInnerSize]
	body := data[pos+udpDataInnerSize : pos+udpDataInnerSize+length]
	plain, err := s.decrypt(seq, body, head)
	if err != nil {
		s.badPackets.Add(1)
		return false
	}
	s.stream.trackGroup(seq, body, fecK)
	fresh := s.stream.deliver(seq, plain)
	// a fresh segment gets the paced ack; a duplicate is answered immediately,
	// because that ack is what unsticks a sender whose own acks were lost
	s.stream.queueAck(!fresh)
	return fresh
}

// notePart remembers a ciphertext so the group's parity can be built.
func (s *udpSession) notePart(seq uint32, cipher []byte, st *udpStream) {
	if s.fecK == 0 || s.fecR == 0 {
		return
	}
	group := seq / uint32(s.fecK)
	bucket := s.sendGroups[group]
	if bucket == nil {
		bucket = map[int][]byte{}
		s.sendGroups[group] = bucket
	}
	bucket[int(seq%uint32(s.fecK))] = cipher
	if len(bucket) >= s.fecK {
		delete(s.sendGroups, group)
		s.emitParity(group, bucket, st)
	}
	for len(s.sendGroups) > udpMaxGroups {
		s.dropOldestSendGroup()
	}
}

// emitParity queues the repair packets of one group.  The caller already holds
// the stream lock (it comes out of sendSegmentLocked), so this must not take it
// again -- a Go mutex is not reentrant.
func (s *udpSession) emitParity(group uint32, parts map[int][]byte, st *udpStream) {
	for parityIndex := 0; parityIndex < s.fecR; parityIndex++ {
		var acc []byte
		ok := true
		for i := 0; i < s.fecK; i++ {
			if i%s.fecR != parityIndex {
				continue
			}
			chunk, have := parts[i]
			if !have {
				ok = false
				break
			}
			if acc == nil {
				acc = append([]byte(nil), chunk...)
				continue
			}
			if len(chunk) != len(acc) {
				ok = false
				break
			}
			for pos := range acc {
				acc[pos] ^= chunk[pos]
			}
		}
		if !ok || acc == nil {
			// a group whose members have different sizes cannot be XORed: the
			// parity is skipped and the loss falls back to the retransmit timer
			continue
		}
		packetID := uint32(udpParityBase) | (group << 8) | uint32(parityIndex)
		head := s.inner(packetID, len(acc), s.fecK, s.fecR, parityIndex)
		packet := make([]byte, 0, udpDataOuterSize+udpDataInnerSize+len(acc))
		packet = append(packet, s.outer()...)
		packet = append(packet, head...)
		packet = append(packet, acc...)
		if len(st.parity) < 4096 {
			st.parity = append(st.parity, packet)
			st.paritySent++
		}
	}
}

func (s *udpSession) dropOldestGroup() {
	var oldest uint32 = ^uint32(0)
	found := false
	for group := range s.recvGroups {
		if !found || group < oldest {
			oldest, found = group, true
		}
	}
	if found {
		delete(s.recvGroups, oldest)
	}
}

func (s *udpSession) dropOldestSendGroup() {
	var oldest uint32 = ^uint32(0)
	found := false
	for group := range s.sendGroups {
		if !found || group < oldest {
			oldest, found = group, true
		}
	}
	if found {
		delete(s.sendGroups, oldest)
	}
}

// ------------------------------------------------------------------ timers

// sendAck writes one ACK packet for the state the stream reports.
func (s *udpSession) sendAck(rcvNxt, bits uint32, rwnd uint16) {
	if s.sid == 0 {
		return
	}
	pkt := make([]byte, udpAckSize)
	pkt[0] = udpAck
	binary.BigEndian.PutUint32(pkt[1:5], s.sid)
	binary.BigEndian.PutUint32(pkt[5:9], rcvNxt)
	binary.BigEndian.PutUint32(pkt[9:13], bits)
	binary.BigEndian.PutUint16(pkt[13:15], rwnd)
	s.send(pkt)
}

// tick runs the retransmit timer and the idle ack; call it every udpTick.
func (s *udpSession) tick() {
	if s.err() != nil {
		return
	}
	st := s.stream
	st.mu.Lock()
	now := time.Now()
	st.onTimeoutLocked(now)
	if st.closed {
		st.mu.Unlock()
		s.fail(st.err)
		return
	}
	st.transmitLocked()
	needIdleAck := len(st.ooo) > 0 && now.Sub(st.lastAckAt) > ackIdle
	rcvNxt := st.rcvNxt
	bits := st.sackBitsLocked()
	rwnd := uint16(st.recvWindowPackets())
	st.mu.Unlock()
	if needIdleAck {
		s.sendAck(rcvNxt, bits, rwnd)
		st.mu.Lock()
		st.lastAckAt = now
		st.pendingAck = 0
		st.mu.Unlock()
	}
}

// ---------------------------------------------------------------------------
// the channel the mux sees

// UDPChannel is a carrier channel to the mux on top of a udp session.
type UDPChannel struct {
	sess *udpSession
	name string
	peer string
}

func (c *UDPChannel) Name() string { return c.name }
func (c *UDPChannel) Peer() string { return c.peer }

func (c *UDPChannel) ReadFrame() (byte, uint32, []byte, error) {
	frame, err := c.sess.stream.readFrame()
	if err != nil {
		return 0, 0, nil, err
	}
	if len(frame) < 1 {
		return 0, 0, nil, errors.New("empty frame")
	}
	typ := frame[0]
	var sid uint32
	var payload []byte
	if len(frame) >= proto.HeaderLen {
		sid = binary.BigEndian.Uint32(frame[1:5])
		payload = frame[proto.HeaderLen:]
	}
	return typ, sid, payload, nil
}

func (c *UDPChannel) WriteData(hdr []byte, payload []byte) error {
	if err := c.sess.err(); err != nil {
		return err
	}
	// one write, so the 4 byte frame length and the header stay together
	buf := make([]byte, 0, 4+len(hdr)+len(payload))
	var length [4]byte
	binary.BigEndian.PutUint32(length[:], uint32(len(hdr)+len(payload)))
	buf = append(buf, length[:]...)
	buf = append(buf, hdr...)
	buf = append(buf, payload...)
	c.sess.stream.write(buf)
	return nil
}

func (c *UDPChannel) WriteFrame(frame []byte) error {
	if err := c.sess.err(); err != nil {
		return err
	}
	c.sess.stream.writeFramed(frame)
	return nil
}

func (c *UDPChannel) Close() error {
	c.sess.sendClose()
	c.sess.fail(errors.New("closed locally"))
	return nil
}

func (s *udpSession) sendClose() {
	if !s.isEstablished() {
		return
	}
	pkt := make([]byte, udpCloseSize)
	pkt[0] = udpClose
	binary.BigEndian.PutUint32(pkt[1:5], s.sid)
	s.send(pkt)
}

// ---------------------------------------------------------------------------
// client

// UDPOptions are the datagram carrier's knobs, matching the Python config.
type UDPOptions struct {
	MTU    int
	FEC    string
	Window int
}

// IsUDPName reports whether a carrier name selects the datagram carrier.
func IsUDPName(name string) bool {
	return strings.EqualFold(strings.TrimSpace(name), "udp")
}

// DialUDP opens the client side of the udp carrier.
func DialUDP(opt ClientOptions, udp UDPOptions) (mux.Channel, error) {
	mtu := clampMTU(orDefault(udp.MTU, udpDefaultMTU))
	fecK, fecR := ParseFEC(udp.FEC)
	window := orDefault(udp.Window, 1024)
	if opt.Timeout <= 0 {
		opt.Timeout = 10 * time.Second
	}
	addr, err := net.ResolveUDPAddr("udp", net.JoinHostPort(opt.Address, fmt.Sprint(opt.Port)))
	if err != nil {
		return nil, err
	}
	conn, err := net.DialUDP("udp", nil, addr)
	if err != nil {
		return nil, err
	}
	capacity := tuneUDPSocket(conn)
	sess := newUDPSession(opt.Token, mtu, fecK, fecR, window,
		func(p []byte) bool {
			if _, err := conn.Write(p); err != nil {
				return false
			}
			return true
		}, false)
	sess.peerAddr = addr
	sess.noteSocketCapacity(capacity)

	// writer goroutine: the handshake and the keepalive loop drive it
	go func() {
		ticker := time.NewTicker(udpTick)
		defer ticker.Stop()
		for range ticker.C {
			if sess.err() != nil {
				return
			}
			sess.tick()
		}
	}()

	// reader goroutine
	readErr := make(chan error, 1)
	go func() {
		buf := make([]byte, 64<<10)
		for {
			n, err := conn.Read(buf)
			if err != nil {
				select {
				case readErr <- err:
				default:
				}
				sess.fail(err)
				return
			}
			if n == 0 {
				continue
			}
			sess.mu.Lock()
			sess.lastRx = time.Now()
			sess.mu.Unlock()
			handleUDPMessage(sess, buf[:n], nil)
		}
	}()

	deadline := time.Now().Add(opt.Timeout)
	backoff := 250 * time.Millisecond
	for !sess.isEstablished() && time.Now().Before(deadline) {
		sess.send(sess.clientHello())
		select {
		case <-sess.established:
		case <-time.After(backoff):
		case err := <-readErr:
			if err != nil {
				conn.Close()
				return nil, err
			}
		}
		if backoff < time.Second {
			backoff = backoff * 16 / 10
		}
	}
	if !sess.isEstablished() {
		conn.Close()
		return nil, errors.New("no answer to the udp handshake (wrong token, " +
			"blocked UDP, or nothing listening on that port)")
	}
	if err := sess.err(); err != nil {
		conn.Close()
		return nil, err
	}
	// idle watchdog
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for range ticker.C {
			sess.mu.Lock()
			last := sess.lastRx
			sess.mu.Unlock()
			if time.Since(last) > udpIdleTimeout {
				sess.fail(errors.New("the udp tunnel went silent"))
				conn.Close()
				return
			}
		}
	}()
	return &UDPChannel{sess: sess, name: "udp", peer: addr.String()}, nil
}

func orDefault(value, fallback int) int {
	if value <= 0 {
		return fallback
	}
	return value
}

// handleUDPMessage routes one datagram inside a session.
func handleUDPMessage(sess *udpSession, data []byte, reply func([]byte) bool) {
	if len(data) == 0 {
		return
	}
	switch data[0] {
	case udpHelloS:
		if !sess.isEstablished() {
			sess.acceptReply(data, udpAuthKey(sess.tokenValue))
		}
	case udpData:
		defer func() {
			if r := recover(); r != nil {
				slogDebug("udp packet error", r)
			}
		}()
		sess.deliverPacket(data)
	case udpAck:
		if len(data) < udpAckSize {
			return
		}
		sid := binary.BigEndian.Uint32(data[1:5])
		if sid != 0 && sess.sid != 0 && sid != sess.sid {
			return
		}
		ack := binary.BigEndian.Uint32(data[5:9])
		bits := binary.BigEndian.Uint32(data[9:13])
		rwnd := binary.BigEndian.Uint16(data[13:15])
		sess.stream.ack(ack, bits, rwnd)
	case udpClose:
		sess.fail(errors.New("the far side closed the udp tunnel"))
	}
}

// tuneUDPSocket asks the kernel for roomy datagram queues and reports what we
// got.  SO_RCVBUFFORCE needs CAP_NET_ADMIN, so this is best effort.
func tuneUDPSocket(conn *net.UDPConn) int {
	_ = conn.SetReadBuffer(udpSockBuffer)
	_ = conn.SetWriteBuffer(udpSockBuffer)
	raw, err := conn.SyscallConn()
	if err != nil {
		return 0
	}
	capacity := 0
	_ = raw.Control(func(fd uintptr) {
		capacity = getsockoptInt(int(fd), soRCVBUF)
	})
	return capacity
}

// ---------------------------------------------------------------------------
// server

// UDPServer is one datagram socket carrying many carrier connections.
type UDPServer struct {
	tokens   [][]byte
	names    []string
	mtu      int
	fecK     int
	fecR     int
	window   int
	capacity int

	conn    *net.UDPConn
	mu      sync.Mutex
	live    map[string]*udpSession
	stop    chan struct{}
	once    sync.Once
	refused atomic.Int64

	onChannel func(mux.Channel)
}

// NewUDPServer builds the listener side of the udp carrier.  Several tokens may
// be configured: any of them authenticates, and the session remembers which.
func NewUDPServer(tokens []string, mtu int, fec string, window int) *UDPServer {
	keys := make([][]byte, 0, len(tokens))
	for _, token := range tokens {
		keys = append(keys, udpAuthKey(token))
	}
	fecK, fecR := ParseFEC(fec)
	return &UDPServer{
		tokens: keys,
		names:  tokens,
		mtu:    clampMTU(orDefault(mtu, udpDefaultMTU)),
		fecK:   fecK,
		fecR:   fecR,
		window: orDefault(window, 1024),
		live:   map[string]*udpSession{},
		stop:   make(chan struct{}),
	}
}

// Serve opens the socket and returns its address; onChannel runs per session.
func (s *UDPServer) Serve(host string, port int, onChannel func(mux.Channel)) (string, error) {
	if len(s.tokens) == 0 {
		return "", &UdpCarrierError{"udp carrier has no token"}
	}
	addr, err := net.ResolveUDPAddr("udp", net.JoinHostPort(host, fmt.Sprint(port)))
	if err != nil {
		return "", err
	}
	conn, err := net.ListenUDP("udp", addr)
	if err != nil {
		return "", err
	}
	s.conn = conn
	s.capacity = tuneUDPSocket(conn)
	s.onChannel = onChannel

	go s.readLoop()
	go s.timerLoop()
	return conn.LocalAddr().String(), nil
}

// Close stops the listener and every session on it.
func (s *UDPServer) Close() error {
	var err error
	s.once.Do(func() {
		close(s.stop)
		if s.conn != nil {
			err = s.conn.Close()
		}
		s.mu.Lock()
		for _, sess := range s.live {
			sess.fail(errors.New("udp listener closed"))
		}
		s.live = map[string]*udpSession{}
		s.mu.Unlock()
	})
	return err
}

// Stats reports the live session count and how many were refused.
func (s *UDPServer) Stats() (live, refused int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.live), int(s.refused.Load())
}

func (s *UDPServer) readLoop() {
	buf := make([]byte, 64<<10)
	for {
		n, addr, err := s.conn.ReadFromUDP(buf)
		if err != nil {
			select {
			case <-s.stop:
				return
			default:
			}
			if ne, ok := err.(net.Error); ok && ne.Temporary() {
				continue
			}
			return
		}
		if n == 0 {
			continue
		}
		s.handle(buf[:n], addr)
	}
}

func (s *UDPServer) handle(data []byte, addr *net.UDPAddr) {
	key := addr.String()
	s.mu.Lock()
	sess := s.live[key]
	switch data[0] {
	case udpHelloC:
		if sess != nil && sess.err() == nil {
			_, cookie, _, _ := udpHelloFields(data)
			if len(cookie) == len(sess.cookie) && hmac.Equal(cookie, sess.cookie) {
				// a re-sent hello: our first answer was lost
				sess.mu.Lock()
				sess.lastRx = time.Now()
				keyCopy := sess.key
				sess.mu.Unlock()
				if keyCopy != nil {
					sess.send(udpHelloPacket(keyCopy, udpHelloS, cookie,
						uint64(time.Now().Unix())))
				}
				s.mu.Unlock()
				return
			}
			sess.fail(errors.New("replaced by a new connection"))
			delete(s.live, key)
			sess = nil
		}
		if sess == nil {
			if len(s.live) >= maxSessions {
				s.refused.Add(1)
				s.mu.Unlock()
				return
			}
			sess = newUDPSession("", s.mtu, s.fecK, s.fecR, s.window,
				func(p []byte) bool { return s.sendTo(addr, p) }, true)
			sess.peerAddr = addr
			sess.noteSocketCapacity(s.capacity)
			s.live[key] = sess
			if !sess.acceptHello(data, s.tokens, s.names) {
				delete(s.live, key)
				s.refused.Add(1)
				s.mu.Unlock()
				return
			}
		}
		channel := &UDPChannel{sess: sess, name: "udp", peer: addr.String()}
		s.mu.Unlock()
		if s.onChannel != nil {
			// its own goroutine: the handler runs the mux for the whole life of
			// the session, and the read loop has to keep reading datagrams
			go s.onChannel(channel)
		}
		return
	case udpData, udpAck, udpClose:
		if sess == nil || sess.err() != nil {
			s.mu.Unlock()
			return
		}
		sess.mu.Lock()
		sess.lastRx = time.Now()
		sess.mu.Unlock()
	}
	s.mu.Unlock()
	handleUDPMessage(sess, data, nil)
}

const maxSessions = 64

func (s *UDPServer) sendTo(addr *net.UDPAddr, data []byte) bool {
	if _, err := s.conn.WriteToUDP(data, addr); err != nil {
		return false
	}
	return true
}

func (s *UDPServer) timerLoop() {
	ticker := time.NewTicker(udpTick)
	defer ticker.Stop()
	for {
		select {
		case <-s.stop:
			return
		case now := <-ticker.C:
			s.mu.Lock()
			for key, sess := range s.live {
				if sess.err() != nil {
					delete(s.live, key)
					continue
				}
				sess.mu.Lock()
				last := sess.lastRx
				sess.mu.Unlock()
				if now.Sub(last) > udpIdleTimeout {
					sess.fail(errors.New("udp session idle"))
					delete(s.live, key)
					continue
				}
				sess.tick()
			}
			s.mu.Unlock()
		}
	}
}
