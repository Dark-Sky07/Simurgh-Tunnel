package node

import (
	"fmt"
	"net"
	"strconv"
	"sync"
	"time"

	"log/slog"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/proto"
)

// UDP forwarding, the same shape as the Python engine: one datagram per frame
// (boundaries are kept), no retransmission, and when the far side is behind the
// datagram is dropped like a full UDP socket would drop it.
//
// On the relay every client address gets its own stream (the exit then uses one
// UDP socket per stream, so replies go back to the right client); flows that
// have been quiet for a while are reaped. Unlike the Python engine a flow that
// could not open its stream yet is kept, with its datagrams buffered, and the
// next datagram tries again -- a relay whose tunnel is reconnecting does not
// black-hole a client for good.

const (
	udpMaxFlows    = 4096
	udpMaxPending  = 64 << 10
	udpMaxDatagram = 65535
	// a flow whose stream is not up yet keeps retrying this often, and gives
	// the buffered datagrams up when they are this old
	udpOpenRetry     = 200 * time.Millisecond
	udpPendingMaxAge = 15 * time.Second
)

// How long a client address keeps its tunnel stream, and how often the reaper
// looks for quiet ones. Variables so the tests can make them short.
var (
	udpFlowTimeout = 60 * time.Second
	udpReapEvery   = 10 * time.Second
)

type udpListener struct {
	relay   *Relay
	mapping config.Mapping
	conn    *net.UDPConn
	counter *mapCounter
	mu      sync.Mutex
	flows   map[string]*udpFlow
	closed  bool
}

type udpFlow struct {
	listener *udpListener
	addr     *net.UDPAddr

	mu           sync.Mutex // guards the fields below (and counts, see counted)
	stream       *mux.Stream
	opening      bool
	pending      [][]byte
	pendingBytes int
	pendingSince time.Time
	last         time.Time
	counted      bool // already counted in the mapping's active flows
}

// ---------------------------------------------------------------------------
// relay side

func (r *Relay) startUDP(m config.Mapping) error {
	host := m.ListenHost
	if host == "" {
		host = "0.0.0.0"
	}
	addr, err := net.ResolveUDPAddr("udp", net.JoinHostPort(host, strconv.Itoa(m.Listen)))
	if err != nil {
		return err
	}
	conn, err := net.ListenUDP("udp", addr)
	if err != nil {
		return err
	}
	ul := &udpListener{relay: r, mapping: m, conn: conn, counter: r.mappingCounter(m.Listen),
		flows: map[string]*udpFlow{}}
	slog.Info("port forwarding", "listen", m.Listen, "target",
		fmt.Sprintf("%s:%d", m.TargetHost, m.TargetPort), "proto", "udp")
	r.udpListen = append(r.udpListen, ul)
	r.wg.Add(2)
	go ul.readLoop()
	go ul.reapLoop()
	return nil
}

func (ul *udpListener) readLoop() {
	defer ul.relay.wg.Done()
	buf := make([]byte, udpMaxDatagram)
	for {
		n, addr, err := ul.conn.ReadFromUDP(buf)
		if err != nil {
			if ul.isClosed() {
				return
			}
			slog.Debug("udp read", "port", ul.mapping.Listen, "err", err)
			return
		}
		if n == 0 {
			continue
		}
		payload := append([]byte(nil), buf[:n]...)
		flow, _ := ul.flowFor(addr)
		if flow == nil {
			continue
		}
		if flow.send(payload) {
			go ul.attach(flow)
		}
	}
}

func (ul *udpListener) isClosed() bool {
	ul.mu.Lock()
	defer ul.mu.Unlock()
	return ul.closed
}

// flowFor returns the flow of one client address, creating it on first sight.
func (ul *udpListener) flowFor(addr *net.UDPAddr) (*udpFlow, bool) {
	key := addr.String()
	ul.mu.Lock()
	defer ul.mu.Unlock()
	if ul.closed {
		return nil, false
	}
	if flow, ok := ul.flows[key]; ok {
		return flow, false
	}
	if len(ul.flows) >= udpMaxFlows {
		return nil, false
	}
	flow := &udpFlow{listener: ul, addr: addr, last: time.Now(), counted: true}
	ul.flows[key] = flow
	ul.counter.conns.Add(1)
	ul.counter.active.Add(1)
	return flow, true
}

// send hands one datagram to the stream, or buffers it while the stream is
// still being opened. It returns true when the caller should open the stream.
func (f *udpFlow) send(data []byte) bool {
	f.mu.Lock()
	f.last = time.Now()
	if f.stream != nil {
		stream := f.stream
		f.mu.Unlock()
		if stream.WriteRaw(data) {
			f.listener.counter.in.Add(int64(len(data)))
			return false
		}
		if !stream.Closed() {
			// the peer is behind: dropping is what a full UDP socket does
			return false
		}
		// the tunnel went away under us: forget the dead stream and open a
		// fresh one for this datagram
		f.mu.Lock()
		if f.stream == stream {
			f.stream = nil
		}
	}
	if f.pendingBytes < udpMaxPending {
		if len(f.pending) == 0 {
			f.pendingSince = time.Now()
		}
		f.pending = append(f.pending, data)
		f.pendingBytes += len(data)
	}
	if f.opening {
		f.mu.Unlock()
		return false
	}
	f.opening = true
	f.mu.Unlock()
	return true
}

// attach opens the tunnel stream for one client address and flushes whatever
// arrived while it was being opened. A failed open is not fatal: the flow keeps
// its buffered datagrams and the next one tries again.
func (ul *udpListener) attach(flow *udpFlow) {
	target, err := proto.EncodeAddr(ul.mapping.TargetHost, ul.mapping.TargetPort)
	if err != nil {
		ul.counter.errors.Add(1)
		ul.closeFlow(flow)
		return
	}
	stream, err := ul.relay.OpenStream(target, proto.ModeUDP)
	if err != nil {
		// the tunnel is not up (yet): keep the flow, so the datagrams already
		// buffered are delivered once it is
		ul.counter.errors.Add(1)
		slog.Debug("udp stream not ready", "port", ul.mapping.Listen, "err", err)
		flow.mu.Lock()
		flow.opening = false
		waiting := len(flow.pending) > 0
		giveUp := waiting && time.Since(flow.pendingSince) > udpPendingMaxAge
		flow.mu.Unlock()
		if giveUp {
			// the client has moved on long ago; stop holding its datagrams
			ul.closeFlow(flow)
			return
		}
		if waiting && !ul.isClosed() {
			// nobody is going to send another datagram just to wake us up
			time.AfterFunc(udpOpenRetry, func() { ul.retry(flow) })
		}
		return
	}
	if ul.isClosed() {
		stream.RST()
		flow.mu.Lock()
		flow.opening = false
		flow.mu.Unlock()
		return
	}

	flow.mu.Lock()
	flow.stream = stream
	pending := flow.pending
	flow.pending = nil
	flow.pendingBytes = 0
	flow.opening = false
	flow.mu.Unlock()

	addr := flow.addr
	stream.Attach(
		func(data []byte) error {
			n, err := ul.conn.WriteToUDP(data, addr)
			if n > 0 {
				ul.counter.out.Add(int64(n))
			}
			return err
		},
		func() { ul.closeFlow(flow) },
		func(error) { ul.closeFlow(flow) },
	)
	for _, datagram := range pending {
		flow.send(datagram) // the stream is live now, nothing to open
	}
}

// retry opens the stream of a flow that is still holding datagrams.
func (ul *udpListener) retry(flow *udpFlow) {
	if ul.isClosed() {
		return
	}
	flow.mu.Lock()
	if flow.stream != nil || flow.opening || len(flow.pending) == 0 {
		flow.mu.Unlock()
		return
	}
	flow.opening = true
	flow.mu.Unlock()
	ul.attach(flow)
}

// closeFlow forgets one client address and closes its stream.
func (ul *udpListener) closeFlow(flow *udpFlow) {
	key := flow.addr.String()
	ul.mu.Lock()
	if current, ok := ul.flows[key]; ok && current == flow {
		delete(ul.flows, key)
		if flow.counted {
			flow.counted = false
			ul.counter.active.Add(-1)
		}
	}
	ul.mu.Unlock()

	flow.mu.Lock()
	stream := flow.stream
	flow.stream = nil
	flow.opening = false
	flow.pending = nil
	flow.pendingBytes = 0
	flow.mu.Unlock()
	if stream != nil {
		stream.RST()
	}
}

func (ul *udpListener) reapLoop() {
	defer ul.relay.wg.Done()
	ticker := time.NewTicker(udpReapEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ul.relay.stop:
			return
		case <-ticker.C:
			cutoff := time.Now().Add(-udpFlowTimeout)
			var stale []*udpFlow
			ul.mu.Lock()
			for _, flow := range ul.flows {
				flow.mu.Lock()
				idle := flow.last.Before(cutoff)
				flow.mu.Unlock()
				if idle {
					stale = append(stale, flow)
				}
			}
			ul.mu.Unlock()
			for _, flow := range stale {
				ul.closeFlow(flow)
			}
		}
	}
}

func (ul *udpListener) close() {
	ul.mu.Lock()
	ul.closed = true
	flows := make([]*udpFlow, 0, len(ul.flows))
	for _, flow := range ul.flows {
		flows = append(flows, flow)
	}
	ul.flows = map[string]*udpFlow{}
	ul.mu.Unlock()
	ul.conn.Close()
	for _, flow := range flows {
		flow.mu.Lock()
		stream := flow.stream
		flow.stream = nil
		flow.mu.Unlock()
		if stream != nil {
			stream.RST()
		}
	}
}

// ---------------------------------------------------------------------------
// exit side

// openUDP wires one tunnel stream to a UDP socket towards the target.
func (e *Exit) openUDP(s *mux.Stream, host string, port int) {
	raddr, err := net.ResolveUDPAddr("udp", net.JoinHostPort(host, strconv.Itoa(port)))
	if err != nil {
		_ = s.Mux.OpenErr(s, proto.ErrBadRequest, "bad udp address")
		return
	}
	conn, err := net.DialUDP("udp", nil, raddr)
	if err != nil {
		_ = s.Mux.OpenErr(s, proto.ErrUnreachable, "cannot reach "+raddr.String())
		return
	}
	if err := s.Mux.OpenOK(s); err != nil {
		conn.Close()
		return
	}
	slog.Debug("udp stream", "sid", s.ID, "to", raddr.String())
	s.Attach(
		func(data []byte) error {
			_, err := conn.Write(data)
			return err
		},
		func() { conn.Close() },
		func(error) { conn.Close() },
	)
	go func() {
		buf := make([]byte, udpMaxDatagram)
		for {
			n, err := conn.Read(buf)
			if n > 0 {
				s.WriteRaw(buf[:n]) // drops when the relay is behind
			}
			if err != nil {
				s.RST()
				return
			}
		}
	}()
}
