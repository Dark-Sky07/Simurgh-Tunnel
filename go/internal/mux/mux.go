// Package mux is the multiplexer: many user connections share a few tunnel
// connections, with credit based flow control and a window that follows the
// bandwidth-delay product so one stream can fill a long (Iran <-> Europe) path.
package mux

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"math/rand"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/proto"
)

const (
	openTimeout   = 20 * time.Second
	windowEvalMin = 50 * time.Millisecond
	defaultRTT    = 120 * time.Millisecond
	pingTimeout   = 30 * time.Second
)

// Channel is one tunnel connection: framed, authenticated, ordered.
type Channel interface {
	// ReadFrame returns the next frame. The payload slice is only valid until
	// the next call, so anything kept must be copied.
	ReadFrame() (typ byte, sid uint32, payload []byte, err error)
	// WriteData writes a DATA frame; the header is fixed per stream so the
	// payload does not have to be glued to it.
	WriteData(hdr []byte, payload []byte) error
	// WriteFrame writes a complete control frame.
	WriteFrame(frame []byte) error
	Close() error
	Name() string
	Peer() string
}

// Stats are the counters both roles report.
type Stats struct {
	BytesIn      atomic.Int64
	BytesOut     atomic.Int64
	FramesIn     atomic.Int64
	FramesOut    atomic.Int64
	StreamsTotal atomic.Int64
	StreamsOpen  atomic.Int64
}

// StreamOpenError carries the peer's answer to an OPEN.
type StreamOpenError struct {
	Msg  string
	Code byte
}

func (e *StreamOpenError) Error() string { return e.Msg }

// Stream is one logical connection inside the tunnel.
type Stream struct {
	Mux    *Mux
	ID     uint32
	Mode   byte
	Target []byte

	closed atomic.Bool
	// half close bookkeeping: eofSent is our own end of stream, remoteEOF is
	// the peer's. The stream only goes away when both are done and the local
	// writer has drained, which is what makes HTTP style half closes work.
	eofMu     sync.Mutex
	eofSent   bool
	remoteEOF bool
	eofGiven  bool // onEOF already told the local socket

	// receive queue (tunnel -> local socket); the mux reader appends, the
	// stream's writer goroutine drains, so a slow client never blocks the
	// tunnel.
	qmu     sync.Mutex
	qcond   *sync.Cond
	queue   [][]byte
	queued  int
	writer  func([]byte) error // where local bytes go
	onEOF   func()
	failed  func(error)
	started bool

	// flow control and window autotuning, guarded by Mux.mu
	sendCredit  int64
	recvUnacked int64
	window      int64
	baseWindow  int64
	maxWindow   int64
	peerCredit  int64
	backlog     int64
	backlogPeak int64
	rateBytes   int64
	rateStart   time.Time

	openDone chan struct{}
	openErr  error
	openOK   bool
}

// Mux multiplexes streams over one channel.
type Mux struct {
	ch      Channel
	IsRelay bool
	Stats   Stats

	mu           sync.Mutex
	cond         *sync.Cond // senders wait for credit here
	streams      map[uint32]*Stream
	nextSID      uint32
	globalSend   int64 // connection-wide credit we may spend
	globalRecv   int64 // bytes received but not yet credited back
	globalWindow int64

	streamWindow    int64
	maxStreamWindow int64
	chunk           int

	rttMu sync.Mutex
	rtt   time.Duration

	onOpen func(*Stream)
	onCtrl func(map[string]any)

	wmu    sync.Mutex // serialises frame writes
	closed atomic.Bool
	done   chan struct{}
}

// Options configure a Mux.
type Options struct {
	StreamWindow    int64
	MaxStreamWindow int64
	GlobalWindow    int64
	Chunk           int
	OnOpen          func(*Stream)
	OnCtrl          func(map[string]any)
}

// New creates a mux over an authenticated channel.
func New(ch Channel, isRelay bool, opt Options) *Mux {
	if opt.StreamWindow <= 0 {
		opt.StreamWindow = proto.DefaultStreamWindow
	}
	if opt.MaxStreamWindow < opt.StreamWindow {
		opt.MaxStreamWindow = opt.StreamWindow
	}
	if opt.GlobalWindow <= 0 {
		opt.GlobalWindow = proto.DefaultGlobalWindow
	}
	if opt.Chunk <= 0 {
		opt.Chunk = proto.DefaultChunk
	}
	m := &Mux{
		ch:              ch,
		IsRelay:         isRelay,
		streams:         make(map[uint32]*Stream),
		globalSend:      opt.GlobalWindow,
		globalWindow:    opt.GlobalWindow,
		streamWindow:    opt.StreamWindow,
		maxStreamWindow: opt.MaxStreamWindow,
		chunk:           opt.Chunk,
		onOpen:          opt.OnOpen,
		onCtrl:          opt.OnCtrl,
		done:            make(chan struct{}),
	}
	m.cond = sync.NewCond(&m.mu)
	if isRelay {
		m.nextSID = 1 // the relay opens odd stream ids
	} else {
		m.nextSID = 2 // the exit opens even ones
	}
	return m
}

// Channel exposes the underlying connection (for logging).
func (m *Mux) Channel() Channel { return m.ch }

// Closed reports whether the tunnel is gone.
func (m *Mux) Closed() bool { return m.closed.Load() }

// RTT is the last measured round-trip time, or zero when unknown.
func (m *Mux) RTT() time.Duration {
	m.rttMu.Lock()
	defer m.rttMu.Unlock()
	return m.rtt
}

func (m *Mux) setRTT(d time.Duration) {
	m.rttMu.Lock()
	m.rtt = d
	m.rttMu.Unlock()
}

// StreamCount is the number of live streams (load balancing between tunnels).
func (m *Mux) StreamCount() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.streams)
}

// Run consumes frames until the channel dies.
func (m *Mux) Run() error {
	defer m.Close()
	var err error
	for {
		typ, sid, payload, rerr := m.ch.ReadFrame()
		if rerr != nil {
			err = rerr
			break
		}
		m.Stats.FramesIn.Add(1)
		m.dispatch(typ, sid, payload)
		if m.closed.Load() {
			err = errors.New("closed")
			break
		}
	}
	return err
}

// Close tears the tunnel and every stream down.
func (m *Mux) Close() {
	if m.closed.Swap(true) {
		return
	}
	m.mu.Lock()
	streams := make([]*Stream, 0, len(m.streams))
	for _, s := range m.streams {
		streams = append(streams, s)
	}
	m.streams = make(map[uint32]*Stream)
	m.cond.Broadcast()
	m.mu.Unlock()
	for _, s := range streams {
		s.finish(true)
	}
	close(m.done)
	m.ch.Close()
}

// Done is closed when the tunnel is gone.
func (m *Mux) Done() <-chan struct{} { return m.done }

// ---------------------------------------------------------------------------
// sending

func (m *Mux) writeFrame(frame []byte) error {
	if m.closed.Load() {
		return errors.New("tunnel closed")
	}
	m.wmu.Lock()
	err := m.ch.WriteFrame(frame)
	m.wmu.Unlock()
	if err != nil {
		m.Close()
	}
	return err
}

func (m *Mux) writeData(hdr, payload []byte) error {
	if m.closed.Load() {
		return errors.New("tunnel closed")
	}
	m.Stats.FramesOut.Add(1)
	m.Stats.BytesOut.Add(int64(len(payload)))
	m.wmu.Lock()
	err := m.ch.WriteData(hdr, payload)
	m.wmu.Unlock()
	if err != nil {
		m.Close()
	}
	return err
}

// OpenOK accepts a stream the peer asked for.
func (m *Mux) OpenOK(s *Stream) error {
	return m.writeFrame(proto.Frame(proto.TOpenOK, s.ID, nil))
}

// OpenErr tells the peer why a stream could not be opened.
func (m *Mux) OpenErr(s *Stream, code byte, text string) error {
	err := m.writeFrame(proto.ErrFrame(s.ID, code, text))
	m.drop(s.ID)
	return err
}

// Ctrl sends a control (JSON) frame.
func (m *Mux) Ctrl(obj any) {
	body, err := json.Marshal(obj)
	if err != nil {
		return
	}
	_ = m.writeFrame(proto.Frame(proto.TCtrl, 0, body))
}

// ---------------------------------------------------------------------------
// streams

// Open asks the peer for a new stream to target (already encoded address).
func (m *Mux) Open(target []byte, mode byte) (*Stream, error) {
	if m.closed.Load() {
		return nil, errors.New("tunnel closed")
	}
	m.mu.Lock()
	sid := m.nextSID
	m.nextSID += 2
	s := newStream(m, sid, mode, target)
	m.streams[sid] = s
	m.Stats.StreamsTotal.Add(1)
	m.Stats.StreamsOpen.Store(int64(len(m.streams)))
	m.mu.Unlock()

	payload := append([]byte{mode}, target...)
	if err := m.writeFrame(proto.Frame(proto.TOpen, sid, payload)); err != nil {
		m.drop(sid)
		return nil, err
	}
	select {
	case <-s.openDone:
	case <-m.done:
		return nil, errors.New("tunnel closed")
	case <-time.After(openTimeout):
		s.RST()
		return nil, &StreamOpenError{Msg: fmt.Sprintf("stream open timeout (stream %d)", sid)}
	}
	if !s.openOK {
		m.drop(sid)
		msg := "open failed"
		code := byte(proto.ErrRefused)
		if e, ok := s.openErr.(*StreamOpenError); ok {
			msg, code = e.Msg, e.Code
		}
		return nil, &StreamOpenError{Msg: msg, Code: code}
	}
	return s, nil
}

// accept registers a stream the peer opened.
func (m *Mux) accept(sid uint32, mode byte, target []byte) *Stream {
	m.mu.Lock()
	s := newStream(m, sid, mode, target)
	m.streams[sid] = s
	m.Stats.StreamsTotal.Add(1)
	m.Stats.StreamsOpen.Store(int64(len(m.streams)))
	m.mu.Unlock()
	return s
}

func (m *Mux) lookup(sid uint32) *Stream {
	m.mu.Lock()
	s := m.streams[sid]
	m.mu.Unlock()
	return s
}

func (m *Mux) drop(sid uint32) {
	m.mu.Lock()
	s, ok := m.streams[sid]
	if ok {
		delete(m.streams, sid)
		m.Stats.StreamsOpen.Store(int64(len(m.streams)))
	}
	m.mu.Unlock()
	if ok {
		s.finish(true)
	}
}

func newStream(m *Mux, sid uint32, mode byte, target []byte) *Stream {
	s := &Stream{
		Mux:        m,
		ID:         sid,
		Mode:       mode,
		Target:     append([]byte(nil), target...),
		window:     m.streamWindow,
		baseWindow: m.streamWindow,
		maxWindow:  m.maxStreamWindow,
		sendCredit: m.streamWindow,
		peerCredit: m.streamWindow,
		rateStart:  time.Now(),
		openDone:   make(chan struct{}),
	}
	s.qcond = sync.NewCond(&s.qmu)
	return s
}

// ---------------------------------------------------------------------------
// Stream: local side

// Attach wires the local socket into the stream: write is where incoming bytes
// go, onEOF fires when the peer half-closes, failed is called on errors.
func (s *Stream) Attach(write func([]byte) error, onEOF func(), failed func(error)) {
	s.qmu.Lock()
	s.writer = write
	s.onEOF = onEOF
	s.failed = failed
	if !s.started {
		s.started = true
		go s.pump()
	}
	s.qmu.Unlock()
	s.qcond.Broadcast()
}

// Write sends local bytes towards the peer, waiting for credit. It blocks, so
// call it from the goroutine that reads the local socket.
func (s *Stream) Write(data []byte) error {
	for len(data) > 0 {
		n := s.reserve(len(data))
		if n == 0 {
			if s.closed.Load() || s.Mux.Closed() {
				return errors.New("stream closed")
			}
			s.waitCredit()
			continue
		}
		if s.closed.Load() {
			return errors.New("stream closed")
		}
		hdr := make([]byte, proto.HeaderLen)
		proto.PutHeader(hdr, proto.TData, s.ID)
		if err := s.Mux.writeData(hdr, data[:n]); err != nil {
			return err
		}
		data = data[n:]
	}
	return nil
}

// reserve takes up to n bytes of credit (stream and connection wide).
func (s *Stream) reserve(n int) int {
	m := s.Mux
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed.Load() {
		return 0
	}
	allow := int64(n)
	if s.sendCredit < allow {
		allow = s.sendCredit
	}
	if m.globalSend < allow {
		allow = m.globalSend
	}
	if allow <= 0 {
		return 0
	}
	s.sendCredit -= allow
	m.globalSend -= allow
	return int(allow)
}

func (s *Stream) waitCredit() {
	m := s.Mux
	m.mu.Lock()
	if !m.closed.Load() && s.sendCredit > 0 && m.globalSend > 0 {
		m.mu.Unlock()
		return
	}
	// a short wait keeps the hot path simple and still wakes on WIN frames
	m.mu.Unlock()
	select {
	case <-time.After(2 * time.Millisecond):
	case <-m.done:
	}
	s.Mux.wakeSenders()
}

func (m *Mux) wakeSenders() {
	m.mu.Lock()
	m.cond.Broadcast()
	m.mu.Unlock()
}

// CloseWrite tells the peer we are done sending (half close).
func (s *Stream) CloseWrite() {
	if s.closed.Load() {
		return
	}
	s.eofMu.Lock()
	if s.eofSent {
		s.eofMu.Unlock()
		return
	}
	s.eofSent = true
	s.eofMu.Unlock()
	_ = s.Mux.writeFrame(proto.Frame(proto.TEof, s.ID, nil))
	s.maybeDrop()
}

// RST hard closes the stream.
func (s *Stream) RST() {
	if s.closed.Swap(true) {
		return
	}
	_ = s.Mux.writeFrame(proto.Frame(proto.TClose, s.ID, nil))
	s.Mux.drop(s.ID)
}

// deliver queues incoming data for the local writer goroutine.
func (s *Stream) deliver(payload []byte) {
	if s.closed.Load() {
		return
	}
	s.qmu.Lock()
	if !s.started {
		// nobody attached yet (the exit answers before the bridge is wired)
		s.queue = append(s.queue, append([]byte(nil), payload...))
		s.queued += len(payload)
		s.qmu.Unlock()
		return
	}
	s.queue = append(s.queue, append([]byte(nil), payload...))
	s.queued += len(payload)
	// A peer that respects credit can have a window's worth of bytes on the
	// way here at any moment, and the autotuner moves that window around while
	// data is in flight, so the safety net only catches real floods: twice the
	// window ceiling plus one chunk.
	limit := s.maxWindow*2 + int64(s.Mux.chunk)
	over := int64(s.queued) > limit
	s.qmu.Unlock()
	s.qcond.Signal()
	if over {
		slog.Warn("peer is flooding a stream, resetting", "sid", s.ID, "queued", s.queued, "limit", limit)
		s.RST()
	}
}

// pump writes queued bytes to the local socket and credits the peer once they
// are really out, which is what keeps the far side from buffering for a slow
// client.
func (s *Stream) pump() {
	for {
		s.qmu.Lock()
		if len(s.queue) > 0 {
			chunk := s.queue[0]
			s.queue = s.queue[1:]
			s.queued -= len(chunk)
			writer := s.writer
			s.qmu.Unlock()
			if writer == nil {
				continue
			}
			if err := writer(chunk); err != nil {
				if s.failed != nil {
					s.failed(err)
				}
				s.RST()
				return
			}
			s.grant(len(chunk))
			s.flushCredit(false)
			continue
		}
		if s.closed.Load() {
			s.qmu.Unlock()
			return
		}
		// everything the peer sent is out: a half close may be passed on now
		if s.remoteEOF && !s.eofGiven {
			s.eofGiven = true
			onEOF := s.onEOF
			s.qmu.Unlock()
			if onEOF != nil {
				onEOF()
			}
			s.maybeDrop()
			continue
		}
		s.qcond.Wait()
		s.qmu.Unlock()
	}
}

// grant counts bytes as consumed (they will be credited back on flush).
func (s *Stream) grant(n int) {
	if n <= 0 {
		return
	}
	m := s.Mux
	m.mu.Lock()
	s.recvUnacked += int64(n)
	m.mu.Unlock()
}

// flushCredit sends a WIN frame when enough credit piled up.
func (s *Stream) flushCredit(force bool) {
	m := s.Mux
	m.mu.Lock()
	n := s.recvUnacked
	if n <= 0 || (!force && n < proto.CreditFlush) {
		m.mu.Unlock()
		return
	}
	s.recvUnacked = 0
	if s.backlog >= n {
		s.backlog -= n
	} else {
		s.backlog = 0
	}
	s.rateBytes += n
	bonus := s.autotuneLocked()
	grant := n + bonus
	s.peerCredit += grant
	global := m.takeGlobalCreditLocked(grant)
	m.mu.Unlock()
	if m.closed.Load() {
		return
	}
	_ = m.writeFrame(proto.Frame(proto.TWin, s.ID, proto.WinPayload(uint32(min64(grant, math.MaxUint32)), uint32(min64(global, math.MaxUint32)))))
}

// takeGlobalCreditLocked answers how much connection-wide credit we may hand
// back: we never let the peer have more than globalWindow bytes in flight.
func (m *Mux) takeGlobalCreditLocked(n int64) int64 {
	room := m.globalWindow - m.globalRecv
	if room < 0 {
		room = 0
	}
	g := n
	if g > room {
		g = room
	}
	m.globalRecv -= g
	if m.globalRecv < 0 {
		m.globalRecv = 0
	}
	return g
}

// autotuneLocked grows the window while the stream is limited by it and keeps
// the memory for idle or blocked streams small. Mirrors the Python rule.
func (s *Stream) autotuneLocked() int64 {
	now := time.Now()
	dt := now.Sub(s.rateStart)
	peak := s.backlogPeak
	s.backlogPeak = s.backlog
	m := s.Mux
	rtt := m.RTT()
	if rtt <= 0 {
		rtt = defaultRTT
	}
	eval := rtt
	if eval < windowEvalMin {
		eval = windowEvalMin
	}
	if dt < eval || s.maxWindow <= s.baseWindow {
		return 0
	}
	rate := float64(s.rateBytes) / dt.Seconds()
	s.rateBytes = 0
	s.rateStart = now
	capacity := float64(s.window) / rtt.Seconds()

	// The queue is not a useful "consumer is behind" signal on its own: with
	// credit based flow control the far side keeps a whole window in flight, so
	// a healthy stream always has about a window queued here. What matters is
	// the rate at which this side really drains. A stream pushing our own
	// ceiling while writes keep up is window limited -> grow. One that drains
	// far below what the window would allow is limited by something else (the
	// client, a rate limit) -> shrink, and never park memory for it.
	if rate >= 0.7*capacity && s.window < s.maxWindow {
		next := s.window * 2
		if next > s.maxWindow {
			next = s.maxWindow
		}
		bonus := next - s.window
		s.window = next
		slog.Debug("window grew", "sid", s.ID, "window", s.window, "rate", fmt.Sprintf("%.1f MB/s", rate/1e6))
		return bonus
	}
	behind := rate < 0.3*capacity && float64(peak) >= float64(s.window)*0.9
	if (rate < 0.15*capacity || behind) && s.window > s.baseWindow {
		s.window /= 2
		if s.window < s.baseWindow {
			s.window = s.baseWindow
		}
		slog.Debug("window shrank", "sid", s.ID, "window", s.window, "rate", fmt.Sprintf("%.1f MB/s", rate/1e6), "behind", behind)
	}
	return 0
}

// finish tears the stream down for good (drop) or marks a clean end when the
// local side is already done.
func (s *Stream) finish(hard bool) {
	if s.closed.Swap(true) {
		return
	}
	s.qmu.Lock()
	failed := s.failed
	s.queue = nil
	s.queued = 0
	s.qmu.Unlock()
	s.qcond.Broadcast()
	if hard && failed != nil {
		failed(errors.New("stream closed"))
	}
}

func (s *Stream) maybeDrop() {
	s.eofMu.Lock()
	both := s.eofSent && s.remoteEOF
	s.eofMu.Unlock()
	if !both {
		return
	}
	s.qmu.Lock()
	idle := len(s.queue) == 0
	s.qmu.Unlock()
	if idle {
		s.Mux.drop(s.ID)
	}
}

// ---------------------------------------------------------------------------
// dispatch

func (m *Mux) dispatch(typ byte, sid uint32, payload []byte) {
	switch typ {
	case proto.TData:
		s := m.lookup(sid)
		if s == nil {
			return
		}
		n := int64(len(payload))
		m.Stats.BytesIn.Add(n)
		m.mu.Lock()
		m.globalRecv += n
		s.peerCredit -= n
		s.backlog += n
		if s.backlog > s.backlogPeak {
			s.backlogPeak = s.backlog
		}
		m.mu.Unlock()
		s.deliver(payload)

	case proto.TWin:
		if len(payload) < 8 {
			return
		}
		s := m.lookup(sid)
		if s == nil {
			return
		}
		grant := int64(binary.BigEndian.Uint32(payload))
		global := int64(binary.BigEndian.Uint32(payload[4:]))
		m.mu.Lock()
		s.sendCredit += grant
		if s.sendCredit > s.maxWindow*2 {
			s.sendCredit = s.maxWindow * 2 // a misbehaving peer cannot grow us
		}
		m.globalSend += global
		m.mu.Unlock()
		m.wakeSenders()

	case proto.TOpen:
		if len(payload) < 1 {
			return
		}
		mode := payload[0]
		s := m.accept(sid, mode, payload[1:])
		if m.onOpen != nil {
			go m.onOpen(s)
		} else {
			_ = m.writeFrame(proto.ErrFrame(sid, proto.ErrDenied, "no handler"))
			m.drop(sid)
		}

	case proto.TOpenOK:
		if s := m.lookup(sid); s != nil {
			s.openOK = true
			select {
			case <-s.openDone:
			default:
				close(s.openDone)
			}
		}

	case proto.TOpenErr:
		if s := m.lookup(sid); s != nil {
			code, text := proto.ParseErrFrame(payload)
			s.openErr = &StreamOpenError{Msg: text, Code: code}
			select {
			case <-s.openDone:
			default:
				close(s.openDone)
			}
		}

	case proto.TEof:
		if s := m.lookup(sid); s != nil {
			s.eofMu.Lock()
			s.remoteEOF = true
			s.eofMu.Unlock()
			// wake the pump: it drains what is left and then closes the local
			// write side, which is the half close the user's socket expects
			s.qcond.Broadcast()
			s.maybeDrop()
		}

	case proto.TClose:
		if s := m.lookup(sid); s != nil {
			m.drop(sid)
		}

	case proto.TPing:
		_ = m.writeFrame(proto.Frame(proto.TPong, 0, payload))

	case proto.TPong:
		if len(payload) >= 8 {
			sent := math.Float64frombits(binary.BigEndian.Uint64(payload[:8]))
			took := time.Since(time.Unix(0, int64(sent*1e9)))
			if took > 0 && took < pingTimeout {
				m.setRTT(took)
			}
		}

	case proto.TCtrl:
		if m.onCtrl != nil && len(payload) > 0 {
			var obj map[string]any
			if err := json.Unmarshal(payload, &obj); err == nil {
				m.onCtrl(obj)
			}
		}
	}
}

// Keepalive sends a PING every interval (jittered) and measures the RTT from
// the PONG, which is what the window autotuner needs.
func (m *Mux) Keepalive(interval time.Duration) {
	if interval <= 0 {
		interval = proto.DefaultKeepalive * time.Second
	}
	for {
		// +-15% so a fleet of tunnels never pings in lockstep
		wait := time.Duration(float64(interval) * (0.85 + rand.Float64()*0.30))
		select {
		case <-time.After(wait):
		case <-m.done:
			return
		}
		stamp := float64(time.Now().UnixNano()) / 1e9
		var b [8]byte
		binary.BigEndian.PutUint64(b[:], math.Float64bits(stamp))
		if err := m.writeFrame(proto.Frame(proto.TPing, 0, b[:])); err != nil {
			return
		}
	}
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}
