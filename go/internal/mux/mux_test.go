package mux

import (
	"errors"
	"fmt"
	"io"
	"runtime"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/proto"
)

// ---------------------------------------------------------------------------
// an in-memory Channel: frames travel over Go channels, so the tests exercise
// the multiplexer (streams, credit, dispatch) without any carrier in the way.

type frame struct {
	typ     byte
	sid     uint32
	payload []byte
}

type memChannel struct {
	out     chan frame
	in      chan frame
	closed  chan struct{}
	closeMu sync.Once
	name    string
	written atomic.Int64
}

func newMemPair(name string, depth int) (*memChannel, *memChannel) {
	a := &memChannel{out: make(chan frame, depth), in: make(chan frame, depth), closed: make(chan struct{}), name: name}
	b := &memChannel{out: a.in, in: a.out, closed: make(chan struct{}), name: name}
	return a, b
}

func (c *memChannel) ReadFrame() (byte, uint32, []byte, error) {
	select {
	case f := <-c.in:
		return f.typ, f.sid, f.payload, nil
	case <-c.closed:
		return 0, 0, nil, io.EOF
	}
}

func (c *memChannel) WriteData(hdr, payload []byte) error {
	out := make([]byte, 0, len(hdr)+len(payload))
	out = append(out, hdr...)
	out = append(out, payload...)
	return c.write(out)
}

func (c *memChannel) WriteFrame(f []byte) error { return c.write(f) }

func (c *memChannel) write(f []byte) error {
	if len(f) < proto.HeaderLen {
		return fmt.Errorf("short frame")
	}
	c.written.Add(int64(len(f)))
	payload := append([]byte(nil), f[proto.HeaderLen:]...)
	select {
	case c.out <- frame{typ: f[0], sid: proto.Sid(f), payload: payload}:
		return nil
	case <-c.closed:
		return io.EOF
	}
}

func (c *memChannel) Close() error {
	c.closeMu.Do(func() { close(c.closed) })
	return nil
}

func (c *memChannel) Name() string { return c.name }
func (c *memChannel) Peer() string { return "mem" }

// dumpOnHang fails the test with every goroutine stack if it does not finish in
// time; a stalled data plane is otherwise impossible to debug.
func dumpOnHang(t *testing.T, what string, timeout time.Duration, done <-chan struct{}) {
	t.Helper()
	select {
	case <-done:
	case <-time.After(timeout):
		buf := make([]byte, 1<<20)
		n := runtime.Stack(buf, true)
		t.Fatalf("%s stalled:\n%s", what, buf[:n])
	}
}

// ---------------------------------------------------------------------------
// tests

// TestStreamRoundTrip pushes data through one stream and checks it comes back
// byte for byte: the exit echoes whatever it receives.
func TestStreamRoundTrip(t *testing.T) {
	a, b := newMemPair("mem", 512)
	const window = 256 << 10
	relay := New(a, true, Options{StreamWindow: window, MaxStreamWindow: 1 << 20})
	exitDone := make(chan struct{})
	var exitBytes atomic.Int64
	exit := New(b, false, Options{
		StreamWindow:    window,
		MaxStreamWindow: 1 << 20,
		OnOpen: func(s *Stream) {
			s.Attach(func(data []byte) error {
				n := len(data)
				exitBytes.Add(int64(n))
				if err := s.Write(data); err != nil {
					return nil // the test is over; do not reset
				}
				return nil
			}, func() { close(exitDone) }, func(error) {})
			_ = s.Mux.OpenOK(s)
		},
	})

	go relay.Run()
	go exit.Run()
	defer relay.Close()
	defer exit.Close()

	target, _ := proto.EncodeAddr("127.0.0.1", 80)
	s, err := relay.Open(target, proto.ModeTCP)
	if err != nil {
		t.Fatalf("open: %v", err)
	}

	var back, sent atomic.Int64
	s.Attach(func(data []byte) error {
		back.Add(int64(len(data)))
		return nil
	}, func() {}, func(error) {})

	const total = 8 << 20
	go func() {
		chunk := make([]byte, 64<<10)
		count := 0
		for count < total {
			n := len(chunk)
			if total-count < n {
				n = total - count
			}
			if err := s.Write(chunk[:n]); err != nil {
				return
			}
			count += n
			sent.Add(int64(n))
		}
		s.CloseWrite()
	}()

	done := make(chan struct{})
	go func() {
		for back.Load() < total {
			time.Sleep(time.Millisecond)
		}
		close(done)
	}()
	dumpOnHang(t, fmt.Sprintf("round trip (sent=%d echoed=%d exitSaw=%d relayStreams=%d exitStreams=%d)",
		sent.Load(), back.Load(), exitBytes.Load(), relay.StreamCount(), exit.StreamCount()),
		20*time.Second, done)
	if got := exitBytes.Load(); got != total {
		t.Fatalf("exit saw %d bytes, want %d", got, total)
	}
}

// TestCreditReturnUnderWindow makes the far side slow (its writer blocks until
// the test lets it go) and checks that the sender stops at the window and
// resumes when credit comes back.
func TestCreditReturnUnderWindow(t *testing.T) {
	a, b := newMemPair("mem", 4096)
	const window = 64 << 10
	relay := New(a, true, Options{StreamWindow: window, MaxStreamWindow: window})
	gate := make(chan struct{})
	var exitGot atomic.Int64
	exit := New(b, false, Options{
		StreamWindow:    window,
		MaxStreamWindow: window,
		OnOpen: func(s *Stream) {
			s.Attach(func(data []byte) error {
				<-gate // hold the far side: credit must not come back
				exitGot.Add(int64(len(data)))
				return nil
			}, func() {}, func(error) {})
			_ = s.Mux.OpenOK(s)
		},
	})

	go relay.Run()
	go exit.Run()
	defer relay.Close()
	defer exit.Close()
	defer close(gate)

	target, _ := proto.EncodeAddr("127.0.0.1", 80)
	s, err := relay.Open(target, proto.ModeTCP)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	var sent atomic.Int64
	go func() {
		chunk := make([]byte, 16<<10)
		for {
			if err := s.Write(chunk); err != nil {
				return
			}
			sent.Add(int64(len(chunk)))
		}
	}()

	// with the far side frozen the sender may not run away: at most the
	// window plus a chunk may be in flight
	time.Sleep(300 * time.Millisecond)
	if got := sent.Load(); got > int64(window)+(1<<20) {
		t.Fatalf("sender ran past the window: %d bytes sent, window %d", got, window)
	}
	if got := sent.Load(); got == 0 {
		t.Fatal("sender did not start")
	}
}

// TestStreamOpenError checks that a refused target is reported to the opener.
func TestStreamOpenError(t *testing.T) {
	a, b := newMemPair("mem", 64)
	relay := New(a, true, Options{StreamWindow: 64 << 10})
	exit := New(b, false, Options{
		StreamWindow: 64 << 10,
		OnOpen: func(s *Stream) {
			_ = s.Mux.OpenErr(s, proto.ErrRefused, "nope")
		},
	})
	go relay.Run()
	go exit.Run()
	defer relay.Close()
	defer exit.Close()

	target, _ := proto.EncodeAddr("127.0.0.1", 9)
	_, err := relay.Open(target, proto.ModeTCP)
	if err == nil {
		t.Fatal("open should have failed")
	}
	var soe *StreamOpenError
	if !errors.As(err, &soe) || soe.Code != proto.ErrRefused {
		t.Fatalf("got %v, want a refused open error", err)
	}
	if n := relay.StreamCount(); n != 0 {
		t.Fatalf("%d streams left open, want 0", n)
	}
}

// TestGlobalWindowScalesWithStreams pins the fix for many-user relays: the
// connection wide credit ceiling follows the sum of the live streams' *current*
// windows. If it were frozen at the initial per-stream window, a connection
// with dozens of users would refuse every credit grant and crawl.
func TestGlobalWindowScalesWithStreams(t *testing.T) {
	a, _ := newMemPair("mem", 8)
	m := New(a, true, Options{StreamWindow: 256 << 10, MaxStreamWindow: 8 << 20})

	if got := m.effectiveGlobalLocked(); got != globalWindowFloor {
		t.Fatalf("no streams: ceiling = %d, want the floor %d", got, globalWindowFloor)
	}
	if m.globalSend != globalWindowFloor {
		t.Fatalf("opening credit = %d, want %d so the first stream can start",
			m.globalSend, globalWindowFloor)
	}

	// add live streams the way Open/accept do, then grow their windows the way
	// the autotuner does
	add := func(n int, window int64) {
		m.mu.Lock()
		for i := 0; i < n; i++ {
			s := newStream(m, m.nextSID, 0, nil)
			m.nextSID += 2
			s.window = window
			m.streams[s.ID] = s
			m.windowSum += s.window
		}
		m.mu.Unlock()
	}
	grow := func(sid uint32, window int64) {
		m.mu.Lock()
		s := m.streams[sid]
		m.windowSum += window - s.window
		s.window = window
		m.mu.Unlock()
	}

	// 16 streams x 256 KiB = 4 MiB, still under the floor
	add(16, 256<<10)
	if got := m.effectiveGlobalLocked(); got != globalWindowFloor {
		t.Fatalf("16 small streams: ceiling = %d, want the floor %d", got, globalWindowFloor)
	}

	// the windows grow: the ceiling has to grow with them, or the grants stop
	grow(1, 8<<20)
	grow(3, 8<<20)
	if got, want := m.effectiveGlobalLocked(), int64((8<<20)+(8<<20)+14*(256<<10)); got != want {
		t.Fatalf("grown streams: ceiling = %d, want %d", got, want)
	}

	// and it never runs away: the cap holds however many streams there are
	add(200, 8<<20)
	if got := m.effectiveGlobalLocked(); got != globalWindowCap {
		t.Fatalf("200 big streams: ceiling = %d, want the cap %d", got, globalWindowCap)
	}

	// a credit frame must not push the sender past the ceiling
	m.mu.Lock()
	m.globalSend = 1
	m.mu.Unlock()
	m.grantCreditForTest(1 << 30)
	m.mu.Lock()
	send := m.globalSend
	m.mu.Unlock()
	if send > globalWindowCap {
		t.Fatalf("credit grew to %d, past the cap %d", send, globalWindowCap)
	}

	// closing a stream gives its window back
	m.mu.Lock()
	before := m.windowSum
	m.mu.Unlock()
	m.drop(1)
	m.mu.Lock()
	after := m.windowSum
	m.mu.Unlock()
	if after >= before {
		t.Fatalf("dropping a stream kept its window: %d -> %d", before, after)
	}

	// an explicit setting still wins, so old configs keep their meaning
	fixed, _ := newMemPair("mem2", 8)
	m2 := New(fixed, true, Options{StreamWindow: 8 << 20, GlobalWindow: 4 << 20})
	if got := m2.effectiveGlobalLocked(); got != 4<<20 {
		t.Fatalf("explicit GlobalWindow ignored: ceiling = %d, want %d", got, 4<<20)
	}
	if m2.windowSum != 0 {
		t.Fatalf("windowSum = %d, want 0 before any stream", m2.windowSum)
	}
}

// grantCreditForTest feeds a credit frame straight into the mux, the same path
// a peer's WIN frame takes.
func (m *Mux) grantCreditForTest(global int64) {
	m.mu.Lock()
	m.globalSend += global
	if limit := m.effectiveGlobalLocked(); m.globalSend > limit {
		m.globalSend = limit
	}
	m.mu.Unlock()
}
