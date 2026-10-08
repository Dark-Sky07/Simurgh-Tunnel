// Package node holds the two roles: the relay on the Iranian side and the exit
// on the foreign side.
package node

import (
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/carrier"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/proto"
)

var bufPool = sync.Pool{
	New: func() any {
		b := make([]byte, 64<<10)
		return &b
	},
}

// Relay is the Iranian side: users connect here and are forwarded through the
// pool of tunnel connections.
// mapCounter is what the panel shows under a port forwarding.
type mapCounter struct {
	in     atomic.Int64
	out    atomic.Int64
	conns  atomic.Int64
	active atomic.Int64
	errors atomic.Int64
}

func (c *mapCounter) asDict() map[string]any {
	return map[string]any{
		"in_bytes":  c.in.Load(),
		"out_bytes": c.out.Load(),
		"conns":     c.conns.Load(),
		"active":    c.active.Load(),
		"errors":    c.errors.Load(),
	}
}

// Relay is the Iranian side: users connect here and are forwarded through the
// pool of tunnel connections.
type Relay struct {
	cfg   *config.Relay
	home  string
	pool  []*mux.Mux
	pmu   sync.Mutex
	stop  chan struct{}
	once  sync.Once
	start time.Time

	dialErrors int
	openErrors int
	lastError  string
	connectCnt int
	listeners  []net.Listener
	tunnel     []net.Listener
	wg         sync.WaitGroup

	statMu   sync.Mutex
	mappings map[string]*mapCounter // key = listen port
	state    *stateWriter
}

// NewRelay builds a relay node.
func NewRelay(cfg *config.Relay, home string) *Relay {
	if home == "" {
		home = "/etc/simurgh"
	}
	return &Relay{cfg: cfg, home: home, stop: make(chan struct{}), start: time.Now(),
		mappings: map[string]*mapCounter{}}
}

// Config returns the loaded configuration.
func (r *Relay) Config() *config.Relay { return r.cfg }

// Start binds the user facing ports and keeps the tunnel pool full.
func (r *Relay) Start() error {
	for _, m := range r.cfg.Mappings {
		if !m.Enabled || m.Listen == 0 {
			continue
		}
		if m.UDP {
			slog.Warn("UDP mappings are not in the Go engine yet; skipping", "port", m.Listen)
			continue
		}
		host := m.ListenHost
		if host == "" {
			host = "0.0.0.0"
		}
		ln, err := net.Listen("tcp", net.JoinHostPort(host, fmt.Sprint(m.Listen)))
		if err != nil {
			slog.Error("cannot listen", "port", m.Listen, "err", err)
			continue
		}
		r.listeners = append(r.listeners, ln)
		slog.Info("port forwarding", "listen", m.Listen, "target", fmt.Sprintf("%s:%d", m.TargetHost, m.TargetPort))
		r.wg.Add(1)
		go r.acceptLoop(ln, m)
	}

	// live numbers for the web panel and `simurgh status`
	r.state = newStateWriter(r.home, "relay", 3*time.Second)
	go r.state.loop(r.stop, r.Status)

	total := r.cfg.Connections
	if total < 1 {
		total = 1
	}
	if r.cfg.Dial == "exit" {
		// reverse mode: the exit dials us; we only accept tunnel connections
		if err := r.startTunnelListener(); err != nil {
			return err
		}
		if total > 1 {
			slog.Info("waiting for the exit to open its pool", "connections", total)
		}
	} else {
		for slot := 0; slot < total; slot++ {
			r.wg.Add(1)
			go r.supervisor(slot)
		}
		if total > 1 {
			slog.Info("keeping tunnel connections to the exit", "connections", total)
		}
	}
	return nil
}

// Stop closes everything.
func (r *Relay) Stop() {
	r.once.Do(func() { close(r.stop) })
	for _, ln := range r.listeners {
		ln.Close()
	}
	for _, ln := range r.tunnel {
		ln.Close()
	}
	r.pmu.Lock()
	muxes := append([]*mux.Mux(nil), r.pool...)
	r.pool = nil
	r.pmu.Unlock()
	for _, m := range muxes {
		m.Close()
	}
	r.wg.Wait()
}

// mappingCounter returns (and remembers) the counters of one port forwarding.
func (r *Relay) mappingCounter(port int) *mapCounter {
	key := fmt.Sprint(port)
	r.statMu.Lock()
	defer r.statMu.Unlock()
	c := r.mappings[key]
	if c == nil {
		c = &mapCounter{}
		r.mappings[key] = c
	}
	return c
}

// ------------------------------------------------------------------ mappings

func (r *Relay) acceptLoop(ln net.Listener, m config.Mapping) {
	defer r.wg.Done()
	counter := r.mappingCounter(m.Listen)
	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-r.stop:
				return
			default:
			}
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			return
		}
		go r.handleUser(counter, conn, m)
	}
}

func (r *Relay) handleUser(counter *mapCounter, conn net.Conn, m config.Mapping) {
	if tc, ok := conn.(*net.TCPConn); ok {
		tc.SetNoDelay(true)
	}
	counter.conns.Add(1)
	counter.active.Add(1)
	defer counter.active.Add(-1)
	conn = countingConn{Conn: conn, counter: counter}
	target, err := proto.EncodeAddr(m.TargetHost, m.TargetPort)
	if err != nil {
		conn.Close()
		return
	}
	s, err := r.OpenStream(target, proto.ModeTCP)
	if err == nil {
		slog.Debug("user stream open", "peer", conn.RemoteAddr(), "sid", s.ID)
	}
	if err != nil {
		counter.errors.Add(1)
		r.openErrors++
		r.lastError = err.Error()
		slog.Warn("cannot open a stream for a user", "peer", conn.RemoteAddr(), "err", err)
		conn.Close()
		return
	}
	Bridge(conn, s)
}

// halfCloser is a socket that can close just its write side (HTTP style half
// close); both a TCP connection and the counting wrapper below satisfy it.
type halfCloser interface {
	CloseWrite() error
}

// countingConn counts the bytes of one user connection for the panel.
type countingConn struct {
	net.Conn
	counter *mapCounter
}

func (c countingConn) Read(p []byte) (int, error) {
	n, err := c.Conn.Read(p)
	if n > 0 {
		c.counter.in.Add(int64(n))
	}
	return n, err
}

func (c countingConn) Write(p []byte) (int, error) {
	n, err := c.Conn.Write(p)
	if n > 0 {
		c.counter.out.Add(int64(n))
	}
	return n, err
}

func (c countingConn) CloseWrite() error {
	if hc, ok := c.Conn.(halfCloser); ok {
		return hc.CloseWrite()
	}
	return nil
}

// Bridge wires a local socket to a tunnel stream in both directions.
func Bridge(conn net.Conn, s *mux.Stream) {
	s.Attach(
		func(data []byte) error {
			_, err := conn.Write(data)
			return err
		},
		func() {
			// the peer finished sending: pass a half close on
			if hc, ok := conn.(halfCloser); ok {
				_ = hc.CloseWrite()
			} else {
				conn.Close()
			}
		},
		func(error) { conn.Close() },
	)

	bufp := bufPool.Get().(*[]byte)
	buf := *bufp
	defer func() {
		bufPool.Put(bufp)
		conn.Close()
	}()
	for {
		n, err := conn.Read(buf)
		if n > 0 {
			if werr := s.Write(buf[:n]); werr != nil {
				return
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				s.CloseWrite()
			} else {
				s.RST()
			}
			return
		}
	}
}

// -------------------------------------------------------------------- tunnel

func (r *Relay) startTunnelListener() error {
	t := r.cfg.Tunnel
	if !t.Enabled {
		return nil
	}
	host := t.Host
	if host == "" {
		host = "0.0.0.0"
	}
	if t.Carrier == "tls" || t.Carrier == "wss" {
		cert, err := carrier.LoadOrCreate(certDir(r.home), r.cfg.Name, t.CertFile, t.KeyFile)
		if err != nil {
			return err
		}
		if fp, err := carrier.Fingerprint(cert); err == nil {
			slog.Info("tunnel certificate", "fingerprint", fp, "hint", "the exit pins this fingerprint")
		}
	}
	ln, err := net.Listen("tcp", net.JoinHostPort(host, fmt.Sprint(t.Port)))
	if err != nil {
		return fmt.Errorf("cannot listen on %s:%d: %w", host, t.Port, err)
	}
	r.tunnel = append(r.tunnel, ln)
	name := t.Carrier
	if name == "" {
		name = "tls"
	}
	slog.Info("waiting for the exit", "listen", fmt.Sprintf("%s://%s:%d", name, host, t.Port))
	r.wg.Add(1)
	go func() {
		defer r.wg.Done()
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go r.serveTunnel(conn)
		}
	}()
	return nil
}

func (r *Relay) serveTunnel(conn net.Conn) {
	t := r.cfg.Tunnel
	cert, err := carrier.LoadOrCreate(certDir(r.home), r.cfg.Name, t.CertFile, t.KeyFile)
	if err != nil {
		conn.Close()
		return
	}
	srv := carrier.NewServer(t.Carrier, r.cfg.Token, cert, t.Fallback, t.DecoyFile)
	srv.Serve(conn, func(ch mux.Channel) {
		m := mux.New(ch, true, mux.Options{
			StreamWindow:    int64(r.cfg.StreamWindow),
			MaxStreamWindow: int64(r.cfg.MaxStreamWindow),
			Chunk:           r.cfg.Chunk,
			OnCtrl:          r.onCtrl,
		})
		if !r.addMux(m, true) {
			m.Close()
			return
		}
		go m.Keepalive(time.Duration(r.cfg.Keepalive) * time.Second)
		slog.Info("exit connected", "peer", ch.Peer(), "tunnels", r.tunnelCount())
		if err := m.Run(); err != nil && !isClosed(err) {
			slog.Debug("tunnel ended", "err", err)
		}
		r.removeMux(m)
	})
}

// ------------------------------------------------------------------ dialling

func (r *Relay) endpoints() []config.Endpoint {
	out := make([]config.Endpoint, 0, 1+len(r.cfg.Pool))
	if r.cfg.Exit.Enabled && r.cfg.Exit.Address != "" {
		out = append(out, r.cfg.Exit)
	}
	for _, e := range r.cfg.Pool {
		if e.Enabled && e.Address != "" {
			out = append(out, e)
		}
	}
	return out
}

func (r *Relay) supervisor(slot int) {
	defer r.wg.Done()
	if slot > 0 {
		time.Sleep(time.Duration(slot) * 150 * time.Millisecond)
	}
	backoff := 500 * time.Millisecond
	eps := r.endpoints()
	if len(eps) == 0 {
		r.lastError = "no exit endpoint configured"
		return
	}
	for {
		select {
		case <-r.stop:
			return
		default:
		}
		ep := eps[time.Now().UnixNano()%int64(len(eps))]
		started := time.Now()
		err := r.runTunnel(ep, slot == 0)
		if time.Since(started) > 30*time.Second {
			backoff = 500 * time.Millisecond
		}
		select {
		case <-r.stop:
			return
		case <-time.After(backoff):
		}
		if backoff < 15*time.Second {
			backoff *= 2
		}
		if err != nil && !isClosed(err) {
			r.dialErrors++
			r.lastError = err.Error()
			slog.Warn("cannot reach the exit", "address", fmt.Sprintf("%s://%s:%d", ep.Carrier, ep.Address, ep.Port), "err", err)
		}
	}
}

func (r *Relay) runTunnel(ep config.Endpoint, primary bool) error {
	ch, err := carrier.Dial(carrier.ClientOptions{
		Carrier:     ep.Carrier,
		Address:     ep.Address,
		Port:        ep.Port,
		Domain:      ep.Domain,
		Path:        ep.Path,
		Fingerprint: ep.Fingerprint,
		Insecure:    ep.InsecureSkipVerify,
		Token:       r.cfg.Token,
		Timeout:     10 * time.Second,
	})
	if err != nil {
		return err
	}
	m := mux.New(ch, true, mux.Options{
		StreamWindow:    int64(r.cfg.StreamWindow),
		MaxStreamWindow: int64(r.cfg.MaxStreamWindow),
		Chunk:           r.cfg.Chunk,
		OnCtrl:          r.onCtrl,
	})
	r.connectCnt++
	if !r.addMux(m, false) {
		m.Close()
		return errors.New("pool is full")
	}
	slog.Info("tunnel up", "target", fmt.Sprintf("%s://%s:%d", ep.Carrier, ep.Address, ep.Port),
		"tunnels", r.tunnelCount())
	if primary {
		mappings := make([]map[string]any, 0, len(r.cfg.Mappings))
		for _, mp := range r.cfg.Mappings {
			mappings = append(mappings, map[string]any{"listen": mp.Listen, "target": mp.TargetPort, "name": mp.Name})
		}
		m.Ctrl(map[string]any{
			"kind":     "hello",
			"name":     r.cfg.Name,
			"mappings": mappings,
			"uptime":   time.Since(r.start).Seconds(),
		})
	}
	go m.Keepalive(time.Duration(r.cfg.Keepalive) * time.Second)
	err = m.Run()
	r.removeMux(m)
	return err
}

func (r *Relay) onCtrl(obj map[string]any) {
	slog.Debug("control message", "kind", obj["kind"])
}

// --------------------------------------------------------------------- pool

func (r *Relay) addMux(m *mux.Mux, replaceOldest bool) bool {
	r.pmu.Lock()
	defer r.pmu.Unlock()
	limit := r.cfg.Connections
	if limit < 1 {
		limit = 1
	}
	if len(r.pool) >= limit {
		if !replaceOldest {
			return false
		}
		old := r.pool[0]
		r.pool = r.pool[1:]
		go old.Close()
	}
	r.pool = append(r.pool, m)
	return true
}

func (r *Relay) removeMux(m *mux.Mux) {
	r.pmu.Lock()
	for i, other := range r.pool {
		if other == m {
			r.pool = append(r.pool[:i], r.pool[i+1:]...)
			break
		}
	}
	r.pmu.Unlock()
}

func (r *Relay) tunnelCount() int {
	r.pmu.Lock()
	defer r.pmu.Unlock()
	return len(r.pool)
}

// Pool returns the live muxes (oldest first).
func (r *Relay) Pool() []*mux.Mux {
	r.pmu.Lock()
	defer r.pmu.Unlock()
	return append([]*mux.Mux(nil), r.pool...)
}

// OpenStream hands the stream to the least busy tunnel, retrying on another
// one when a tunnel dies mid-open.
func (r *Relay) OpenStream(target []byte, mode byte) (*mux.Stream, error) {
	r.pmu.Lock()
	live := append([]*mux.Mux(nil), r.pool...)
	r.pmu.Unlock()
	if len(live) == 0 {
		return nil, errors.New("tunnel is not connected yet")
	}
	sort.SliceStable(live, func(i, j int) bool { return live[i].StreamCount() < live[j].StreamCount() })
	var last error
	for _, m := range live {
		if m.Closed() {
			continue
		}
		s, err := m.Open(target, mode)
		if err == nil {
			return s, nil
		}
		last = err
		var soe *mux.StreamOpenError
		if errors.As(err, &soe) && soe.Code != proto.ErrRefused {
			// the target itself refused: trying another tunnel will not help
			return nil, err
		}
	}
	if last == nil {
		last = errors.New("no usable tunnel")
	}
	return nil, last
}

// Status is the snapshot the web panel and `simurgh status` show: it keeps the
// same field names the Python engine uses, so both engines feed one panel.
func (r *Relay) Status() map[string]any {
	r.pmu.Lock()
	pool := append([]*mux.Mux(nil), r.pool...)
	r.pmu.Unlock()

	var in, out int64
	streams := 0
	var rtt time.Duration
	for i, m := range pool {
		in += m.Stats.BytesIn.Load()
		out += m.Stats.BytesOut.Load()
		streams += m.StreamCount()
		if i == 0 {
			rtt = m.RTT()
		}
	}

	// user facing traffic is counted per mapping; add it to the totals
	r.statMu.Lock()
	mappingStats := make(map[string]*mapCounter, len(r.mappings))
	for k, v := range r.mappings {
		mappingStats[k] = v
	}
	r.statMu.Unlock()
	mappings := make([]map[string]any, 0, len(r.cfg.Mappings))
	active := 0
	for _, m := range r.cfg.Mappings {
		c := mappingStats[fmt.Sprint(m.Listen)]
		entry := map[string]any{
			"key":     fmt.Sprintf("tcp:%s:%d/%s:%d", m.ListenHost, m.Listen, m.TargetHost, m.TargetPort),
			"name":    m.Name,
			"listen":  m.Listen,
			"target":  fmt.Sprintf("%s:%d", m.TargetHost, m.TargetPort),
			"udp":     m.UDP,
			"enabled": m.Enabled,
			"bound":   r.listening(m.Listen),
		}
		if c != nil {
			stat := c.asDict()
			entry["stats"] = stat
			in += c.in.Load()
			out += c.out.Load()
			active += int(c.active.Load())
		}
		mappings = append(mappings, entry)
	}

	endpoint := ""
	if eps := r.endpoints(); len(eps) > 0 {
		ep := eps[0]
		endpoint = fmt.Sprintf("%s://%s:%d", ep.Carrier, ep.Address, ep.Port)
	}
	targets := make([]string, 0, len(pool))
	for range pool {
		targets = append(targets, endpoint)
	}
	tunnelListen := []string{}
	if r.cfg.Dial == "exit" && r.cfg.Tunnel.Enabled {
		tunnelListen = append(tunnelListen,
			fmt.Sprintf("%s://%s:%d", r.cfg.Tunnel.Carrier, r.cfg.Tunnel.Host, r.cfg.Tunnel.Port))
	}
	name := r.cfg.Name
	if name == "" {
		name = "relay"
	}
	return map[string]any{
		"role":           "relay",
		"name":           name,
		"engine":         "go",
		"connected":      len(pool) > 0,
		"current_exit":   endpoint,
		"connections":    len(pool),
		"tunnel_targets": targets,
		"rtt_ms":         float64(rtt.Microseconds()) / 1000.0,
		"uptime":         time.Since(r.start).Seconds(),
		"reconnects":     r.connectCnt,
		"last_error":     r.lastError,
		"bind_errors":    []string{},
		"dial":           r.cfg.Dial,
		"tunnel_listen":  tunnelListen,
		"exit_info":      map[string]any{},
		"streams":        streams,
		"mappings":       mappings,
		"stats": map[string]any{
			"totals": map[string]any{"in": in, "out": out, "sum": in + out, "conns": active},
			"tunnel": map[string]any{"in_bytes": in, "out_bytes": out, "conns": active},
		},
	}
}

// listening reports whether a user port is bound right now.
func (r *Relay) listening(port int) bool {
	want := fmt.Sprint(port)
	for _, ln := range r.listeners {
		if _, p, err := net.SplitHostPort(ln.Addr().String()); err == nil && p == want {
			return true
		}
	}
	return false
}

func isClosed(err error) bool {
	if err == nil {
		return true
	}
	if errors.Is(err, net.ErrClosed) || errors.Is(err, io.EOF) {
		return true
	}
	return false
}
