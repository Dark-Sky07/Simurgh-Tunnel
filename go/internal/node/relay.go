// Package node holds the two roles: the relay on the Iranian side and the exit
// on the foreign side.
package node

import (
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"path/filepath"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/carrier"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/limits"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/proto"
)

// ThrottleRate is what a mapping over a "throttle" quota drops to: still
// online, but at a crawl until the period rolls over.
const ThrottleRate = 128 * 1024

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
	in        atomic.Int64
	out       atomic.Int64
	conns     atomic.Int64
	active    atomic.Int64
	errors    atomic.Int64
	lastError atomic.Value // string
}

func (c *mapCounter) asDict() map[string]any {
	err := ""
	if v, ok := c.lastError.Load().(string); ok {
		err = v
	}
	return map[string]any{
		"in_bytes":   c.in.Load(),
		"out_bytes":  c.out.Load(),
		"conns":      c.conns.Load(),
		"active":     c.active.Load(),
		"errors":     c.errors.Load(),
		"last_error": err,
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

	listeners []net.Listener
	tunnel    []net.Listener
	udpTunnel []*carrier.UDPServer
	udpListen []*udpListener
	wg        sync.WaitGroup

	// counters the supervisor goroutines write and the status reader reads
	cmu        sync.Mutex
	dialErrors int
	openErrors int
	lastError  string
	connectCnt int

	statMu   sync.Mutex
	mappings map[string]*mapCounter // key = listen port
	state    *stateWriter

	// bandwidth limits, quotas and the traffic ledger (see go/internal/limits)
	limMu   sync.Mutex
	buckets map[string]*limits.Bucket
	quotas  map[string]limits.Quota
	warned  map[string]time.Time
	active  map[string]*atomic.Int64
	blocked atomic.Int64
	account *limits.Account
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
	// the ledger survives restarts and is the same file the Python engine
	// writes, so `simurgh traffic` keeps its history across an engine switch
	r.account = limits.Open(filepath.Join(r.home, "traffic.json"))
	r.account.Start()
	defer r.account.Prune(limits.KeepDays)

	for _, m := range r.cfg.Mappings {
		if !m.Enabled || m.Listen == 0 {
			continue
		}
		if m.UDP {
			if err := r.startUDP(m); err != nil {
				slog.Error("cannot listen on udp", "port", m.Listen, "err", err)
			}
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
	if r.account != nil {
		defer r.account.Stop()
	}
	for _, ln := range r.listeners {
		ln.Close()
	}
	for _, ln := range r.tunnel {
		ln.Close()
	}
	for _, srv := range r.udpTunnel {
		srv.Close()
	}
	for _, ul := range r.udpListen {
		ul.close()
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
	key := m.Key()
	// a mapping over its quota either stops accepting or crawls
	bucket := r.limiterFor(m)
	if r.overQuota(m) {
		if r.quotaFor(m).Action == "throttle" {
			bucket = limits.NewBucket(ThrottleRate, 0, key)
		} else {
			r.blocked.Add(1)
			counter.errors.Add(1)
			counter.lastError.Store("quota exceeded")
			slog.Warn("mapping is over its quota; refusing a user",
				"mapping", m.Name, "key", key, "peer", conn.RemoteAddr())
			conn.Close()
			return
		}
	}
	if !r.takeSlot(m, key) {
		counter.errors.Add(1)
		counter.lastError.Store("max_conns reached")
		slog.Warn("mapping is at max_conns; refusing a user",
			"mapping", m.Name, "limit", m.MaxConns, "peer", conn.RemoteAddr())
		conn.Close()
		return
	}
	counter.conns.Add(1)
	counter.active.Add(1)
	defer func() {
		counter.active.Add(-1)
		r.releaseSlot(key)
	}()
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
		r.cmu.Lock()
		r.openErrors++
		r.lastError = err.Error()
		r.cmu.Unlock()
		slog.Warn("cannot open a stream for a user", "peer", conn.RemoteAddr(), "err", err)
		conn.Close()
		return
	}
	BridgeLimited(conn, s, bucket, func(up bool, n int) {
		if up {
			r.countBytes(key, int64(n), 0)
		} else {
			r.countBytes(key, 0, int64(n))
		}
	})
}

// ------------------------------------------------------------------- limits

// limiterFor is the shared byte bucket of one mapping (nil = unlimited).
func (r *Relay) limiterFor(m config.Mapping) *limits.Bucket {
	key := m.Key()
	r.limMu.Lock()
	defer r.limMu.Unlock()
	if r.buckets == nil {
		r.buckets = map[string]*limits.Bucket{}
	}
	if bucket, ok := r.buckets[key]; ok {
		return bucket
	}
	value := m.RateLimit
	if value == nil {
		value = r.cfg.RateLimit
	}
	bucket := limits.Limiter(value, key)
	if bucket != nil {
		slog.Info("rate limit", "mapping", m.Name, "key", key,
			"rate", limits.Human(float64(bucket.Rate))+"/s")
	}
	r.buckets[key] = bucket
	return bucket
}

// quotaFor is the quota of one mapping, falling back to the node default.
func (r *Relay) quotaFor(m config.Mapping) limits.Quota {
	key := m.Key()
	r.limMu.Lock()
	defer r.limMu.Unlock()
	if r.quotas == nil {
		r.quotas = map[string]limits.Quota{}
	}
	if quota, ok := r.quotas[key]; ok {
		return quota
	}
	value, period, action := m.Quota, m.QuotaPeriod, m.QuotaAction
	if value == nil {
		value, period, action = r.cfg.Quota, r.cfg.QuotaPeriod, r.cfg.QuotaAction
	}
	quota := limits.QuotaFrom(value, period, action, key)
	if quota.Enabled() {
		slog.Info("traffic quota", "mapping", m.Name, "key", key, "quota", quota.Describe())
	}
	r.quotas[key] = quota
	return quota
}

// overQuota reports whether a mapping has used up its allowance.  A "warn"
// quota never blocks; it just says so once per five minutes.
func (r *Relay) overQuota(m config.Mapping) bool {
	quota := r.quotaFor(m)
	if r.account == nil || !quota.Enabled() {
		return false
	}
	key := m.Key()
	if !quota.Exceeded(r.account, key) {
		return false
	}
	if quota.Action == "warn" {
		r.limMu.Lock()
		if r.warned == nil {
			r.warned = map[string]time.Time{}
		}
		last := r.warned[key]
		r.warned[key] = time.Now()
		r.limMu.Unlock()
		if time.Since(last) > 5*time.Minute {
			slog.Warn("mapping is over its quota", "key", key,
				"used", r.account.Usage(key, quota.Period), "limit", quota.Limit)
		}
		return false
	}
	return true
}

// takeSlot reserves one of the mapping's concurrent connections.
func (r *Relay) takeSlot(m config.Mapping, key string) bool {
	if m.MaxConns <= 0 {
		return true
	}
	r.limMu.Lock()
	if r.active == nil {
		r.active = map[string]*atomic.Int64{}
	}
	slot := r.active[key]
	if slot == nil {
		slot = &atomic.Int64{}
		r.active[key] = slot
	}
	r.limMu.Unlock()
	for {
		current := slot.Load()
		if current >= int64(m.MaxConns) {
			return false
		}
		if slot.CompareAndSwap(current, current+1) {
			return true
		}
	}
}

func (r *Relay) releaseSlot(key string) {
	r.limMu.Lock()
	slot := r.active[key]
	r.limMu.Unlock()
	if slot != nil {
		slot.Add(-1)
	}
}

// countBytes writes traffic into the ledger (and keeps the ledger's totals
// visible in the panel as it goes).
func (r *Relay) countBytes(key string, in, out int64) {
	if r.account != nil {
		r.account.Add(key, in, out)
	}
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

// Paced is the part of *limits.Bucket a data path needs: how many bytes may
// pass now, and how long to wait for the rest.
type Paced interface {
	Allowance(n int) (int, time.Duration)
}

// Bridge wires a local socket to a tunnel stream in both directions.
func Bridge(conn net.Conn, s *mux.Stream) {
	BridgeLimited(conn, s, nil, nil)
}

// BridgeLimited is Bridge with a bandwidth limit and a byte counter.
//
// The limit is applied on both directions of the same connection and both draw
// from one bucket, so `rate_limit = "20M"` really means 20 Mbit/s for that map
// (10 Mbit/s each way when both directions run at once), like frp's
// bandwidthLimit.  When the bucket runs dry the data path sleeps a moment
// instead of dropping anything, so a limited user sees a slower link, never a
// broken one.
func BridgeLimited(conn net.Conn, s *mux.Stream, paced Paced, onBytes func(up bool, n int)) {
	count := func(up bool, n int) {
		if onBytes != nil && n > 0 {
			onBytes(up, n)
		}
	}
	// down: the stream's own pump goroutine calls this, so sleeping here
	// backpressures the tunnel instead of stalling the mux reader
	s.Attach(
		func(data []byte) error {
			if err := writePaced(conn, data, paced); err != nil {
				return err
			}
			count(false, len(data))
			return nil
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
			count(true, n)
			if werr := writeStream(s, buf[:n], paced); werr != nil {
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

// writePaced writes every byte of data to w, waiting on the bucket when it has
// to.  A partial allowance is written straight away, so a frame bigger than
// the burst still makes progress.
func writePaced(w io.Writer, data []byte, paced Paced) error {
	for len(data) > 0 {
		if paced == nil {
			_, err := w.Write(data)
			return err
		}
		allowed, delay := paced.Allowance(len(data))
		if allowed > 0 {
			if _, err := w.Write(data[:allowed]); err != nil {
				return err
			}
			data = data[allowed:]
		}
		if len(data) > 0 {
			if delay <= 0 {
				delay = time.Millisecond
			}
			time.Sleep(delay)
		}
	}
	return nil
}

// writeStream is writePaced for a tunnel stream (whose Write takes the whole
// chunk and waits for window credit).
func writeStream(s *mux.Stream, data []byte, paced Paced) error {
	for len(data) > 0 {
		if paced == nil {
			return s.Write(data)
		}
		allowed, delay := paced.Allowance(len(data))
		if allowed > 0 {
			if err := s.Write(data[:allowed]); err != nil {
				return err
			}
			data = data[allowed:]
		}
		if len(data) > 0 {
			if delay <= 0 {
				delay = time.Millisecond
			}
			time.Sleep(delay)
		}
	}
	return nil
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
	if carrier.IsUDPName(t.Carrier) {
		srv := carrier.NewUDPServer(r.cfg.TokenValues(), t.MTU, t.FEC, t.Window)
		addr, err := srv.Serve(host, t.Port, r.serveTunnelChannel)
		if err != nil {
			return fmt.Errorf("cannot listen on %s:%d: %w", host, t.Port, err)
		}
		r.udpTunnel = append(r.udpTunnel, srv)
		slog.Info("waiting for the exit", "listen", "udp://"+addr, "fec", t.FEC)
		return nil
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
	srv.Tokens = r.cfg.TokenValues()
	srv.Padding = t.Padding
	srv.Serve(conn, r.serveTunnelChannel)
}

// serveTunnelChannel adopts one authenticated reverse tunnel, whatever carried
// it here.
func (r *Relay) serveTunnelChannel(ch mux.Channel) {
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
		r.cmu.Lock()
		r.lastError = "no exit endpoint configured"
		r.cmu.Unlock()
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
			r.cmu.Lock()
			r.dialErrors++
			r.lastError = err.Error()
			r.cmu.Unlock()
			slog.Warn("cannot reach the exit", "address", fmt.Sprintf("%s://%s:%d", ep.Carrier, ep.Address, ep.Port), "err", err)
		}
	}
}

func (r *Relay) runTunnel(ep config.Endpoint, primary bool) error {
	ch, err := endpointTarget(ep, r.cfg.Token).dial()
	if err != nil {
		return err
	}
	m := mux.New(ch, true, mux.Options{
		StreamWindow:    int64(r.cfg.StreamWindow),
		MaxStreamWindow: int64(r.cfg.MaxStreamWindow),
		Chunk:           r.cfg.Chunk,
		OnCtrl:          r.onCtrl,
	})
	r.cmu.Lock()
	r.connectCnt++
	r.cmu.Unlock()
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
	r.cmu.Lock()
	connectCnt, lastError := r.connectCnt, r.lastError
	r.cmu.Unlock()

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
		protoName := "tcp"
		if m.UDP {
			protoName = "udp"
		}
		entry := map[string]any{
			"key":     fmt.Sprintf("%s:%s:%d/%s:%d", protoName, m.ListenHost, m.Listen, m.TargetHost, m.TargetPort),
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
		"reconnects":     connectCnt,
		"last_error":     lastError,
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
		"traffic": r.trafficRows(),
		"ledger":  r.ledgerSnapshot(),
		"blocked": r.blocked.Load(),
	}
}

// trafficRows is the per-key traffic table the panel shows.
func (r *Relay) trafficRows() []map[string]any {
	if r.account == nil {
		return []map[string]any{}
	}
	return r.account.Rows("month")
}

// ledgerSnapshot is the raw counters (today / this month / all time).
func (r *Relay) ledgerSnapshot() map[string]any {
	if r.account == nil {
		return map[string]any{}
	}
	return r.account.Snapshot()
}

// Limits exposes the live buckets for the panel.
func (r *Relay) Limits() map[string]any {
	r.limMu.Lock()
	defer r.limMu.Unlock()
	out := map[string]any{}
	for key, bucket := range r.buckets {
		if bucket != nil {
			out[key] = bucket.Snapshot()
		}
	}
	return out
}

// listening reports whether a user port is bound right now.
func (r *Relay) listening(port int) bool {
	want := fmt.Sprint(port)
	for _, ln := range r.listeners {
		if _, p, err := net.SplitHostPort(ln.Addr().String()); err == nil && p == want {
			return true
		}
	}
	for _, ul := range r.udpListen {
		if _, p, err := net.SplitHostPort(ul.conn.LocalAddr().String()); err == nil && p == want {
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
