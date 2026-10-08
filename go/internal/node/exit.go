package node

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"sync"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/carrier"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/proto"
)

// Exit is the foreign side: it accepts tunnel connections (direct mode) or
// dials the relay (reverse mode) and connects streams to the real services.
type Exit struct {
	cfg *config.Exit
	home string

	mu        sync.Mutex
	tunnels   []*mux.Mux
	peers     map[*mux.Mux]string
	started   map[*mux.Mux]time.Time
	listeners []net.Listener
	state     *stateWriter
	endpoint  []string

	stop       chan struct{}
	once       sync.Once
	start      time.Time
	wg         sync.WaitGroup
	dialErrors int
	lastError  string
	localIPs   map[string]bool
}

// NewExit builds an exit node.
func NewExit(cfg *config.Exit, home string) *Exit {
	local := map[string]bool{}
	if ifaces, err := net.InterfaceAddrs(); err == nil {
		for _, a := range ifaces {
			if ipnet, ok := a.(*net.IPNet); ok {
				local[ipnet.IP.String()] = true
			}
		}
	}
	if home == "" {
		home = "/etc/simurgh"
	}
	return &Exit{cfg: cfg, home: home, stop: make(chan struct{}), start: time.Now(),
		peers: map[*mux.Mux]string{}, started: map[*mux.Mux]time.Time{}, localIPs: local}
}

// Config returns the loaded configuration.
func (e *Exit) Config() *config.Exit { return e.cfg }

// Start brings up every configured endpoint.
func (e *Exit) Start() error {
	total := e.cfg.Connections
	if total < 1 {
		total = 1
	}
	// live numbers for the web panel and `simurgh status`
	e.state = newStateWriter(e.home, "exit", 3*time.Second)
	go e.state.loop(e.stop, e.Status)

	for _, spec := range e.cfg.Listen {
		if !spec.Enabled {
			continue
		}
		if spec.Dial != "" {
			// reverse mode: the relay listens, we dial it and keep it up
			slog.Info("dialling the relay", "address", fmt.Sprintf("%s://%s:%d", spec.Carrier, spec.Dial, spec.Port),
				"connections", total)
			for slot := 0; slot < total; slot++ {
				e.wg.Add(1)
				go e.reverseSupervisor(spec, slot)
			}
			continue
		}
		if err := e.startListener(spec); err != nil {
			slog.Error("cannot listen", "port", spec.Port, "err", err)
		}
	}
	return nil
}

// Stop closes the listeners and every tunnel.
func (e *Exit) Stop() {
	e.once.Do(func() { close(e.stop) })
	for _, ln := range e.listeners {
		ln.Close()
	}
	e.mu.Lock()
	tunnels := append([]*mux.Mux(nil), e.tunnels...)
	e.tunnels = nil
	e.mu.Unlock()
	for _, m := range tunnels {
		m.Close()
	}
	e.wg.Wait()
}

func (e *Exit) serverCert(spec config.ListenSpec) (tls.Certificate, error) {
	return carrier.LoadOrCreate(certDir(e.home), e.cfg.Name,
		pick(spec.CertFile, e.cfg.CertFile), pick(spec.KeyFile, e.cfg.KeyFile))
}

func pick(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

func certDir(home string) string { return home + "/cert" }

func (e *Exit) startListener(spec config.ListenSpec) error {
	host := spec.Host
	if host == "" {
		host = "0.0.0.0"
	}
	ln, err := net.Listen("tcp", net.JoinHostPort(host, fmt.Sprint(spec.Port)))
	if err != nil {
		return err
	}
	e.listeners = append(e.listeners, ln)
	e.endpoint = append(e.endpoint, fmt.Sprintf("%s://%s:%d", spec.Carrier, host, spec.Port))
	var srv *carrier.Server
	if spec.Carrier == "tls" || spec.Carrier == "wss" {
		cert, err := e.serverCert(spec)
		if err != nil {
			return err
		}
		srv = carrier.NewServer(spec.Carrier, e.cfg.Token, cert, spec.Fallback)
		if fp, err := carrier.Fingerprint(cert); err == nil {
			slog.Info("listening", "carrier", spec.Carrier, "address", ln.Addr().String(),
				"probers see", decoyMessage(spec.Fallback), "fingerprint", fp)
		}
	} else {
		srv = carrier.NewServer(spec.Carrier, e.cfg.Token, tls.Certificate{}, spec.Fallback)
		slog.Info("listening", "carrier", spec.Carrier, "address", ln.Addr().String(), "probers see", "noise")
	}
	e.wg.Add(1)
	go func() {
		defer e.wg.Done()
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go srv.Serve(conn, e.onChannel)
		}
	}()
	return nil
}

func decoyMessage(fallback string) string {
	if fallback == "close" {
		return "silent close"
	}
	return "decoy website"
}

// -------------------------------------------------------------------- tunnel

func (e *Exit) onChannel(ch mux.Channel) {
	m := mux.New(ch, false, mux.Options{
		StreamWindow:    int64(e.cfg.StreamWindow),
		MaxStreamWindow: int64(e.cfg.MaxStreamWindow),
		Chunk:           e.cfg.Chunk,
		OnOpen:          e.handleOpen,
	})
	e.mu.Lock()
	e.tunnels = append(e.tunnels, m)
	e.peers[m] = ch.Peer()
	e.started[m] = time.Now()
	count := len(e.tunnels)
	e.mu.Unlock()
	slog.Info("tunnel established", "peer", ch.Peer(), "carrier", ch.Name(), "tunnels", count)
	go m.Keepalive(time.Duration(e.cfg.Keepalive) * time.Second)
	err := m.Run()
	e.mu.Lock()
	for i, other := range e.tunnels {
		if other == m {
			e.tunnels = append(e.tunnels[:i], e.tunnels[i+1:]...)
			break
		}
	}
	delete(e.peers, m)
	delete(e.started, m)
	left := len(e.tunnels)
	e.mu.Unlock()
	if err != nil {
		e.lastError = err.Error()
	}
	slog.Warn("tunnel closed", "peer", ch.Peer(), "left", left, "err", err)
}

// handleOpen connects one stream to the service behind this exit.
func (e *Exit) handleOpen(s *mux.Stream) {
	if s.Mode == proto.ModeUDP {
		_ = s.Mux.OpenErr(s, proto.ErrBadRequest, "udp is not in the Go engine yet")
		return
	}
	host, port, _, err := proto.DecodeAddr(s.Target)
	if err != nil {
		_ = s.Mux.OpenErr(s, proto.ErrBadRequest, "bad address")
		return
	}
	dialer := net.Dialer{Timeout: 10 * time.Second}
	conn, err := dialer.DialContext(context.Background(), "tcp", net.JoinHostPort(host, fmt.Sprint(port)))
	if err != nil {
		code := proto.ErrRefused
		var ne net.Error
		if errors.As(err, &ne) && ne.Timeout() {
			code = proto.ErrTimeout
		}
		_ = s.Mux.OpenErr(s, code, "cannot reach "+net.JoinHostPort(host, fmt.Sprint(port)))
		return
	}
	if tc, ok := conn.(*net.TCPConn); ok {
		tc.SetNoDelay(true)
	}
	if err := s.Mux.OpenOK(s); err != nil {
		conn.Close()
		return
	}
	slog.Debug("stream", "sid", s.ID, "to", net.JoinHostPort(host, fmt.Sprint(port)))
	Bridge(conn, s)
}

// ------------------------------------------------------------- reverse dials

func (e *Exit) reverseSupervisor(spec config.ListenSpec, slot int) {
	defer e.wg.Done()
	if slot > 0 {
		time.Sleep(time.Duration(slot) * 150 * time.Millisecond)
	}
	backoff := 500 * time.Millisecond
	for {
		select {
		case <-e.stop:
			return
		default:
		}
		started := time.Now()
		err := e.reverseTunnel(spec)
		if time.Since(started) > 30*time.Second {
			backoff = 500 * time.Millisecond
		}
		select {
		case <-e.stop:
			return
		case <-time.After(backoff):
		}
		if backoff < 15*time.Second {
			backoff *= 2
		}
		if err != nil && !isClosed(err) {
			e.dialErrors++
			e.lastError = err.Error()
			slog.Warn("cannot reach the relay", "address", fmt.Sprintf("%s->%s:%d", spec.Carrier, spec.Dial, spec.Port), "err", err)
		}
	}
}

func (e *Exit) reverseTunnel(spec config.ListenSpec) error {
	ch, err := carrier.Dial(carrier.ClientOptions{
		Carrier:     spec.Carrier,
		Address:     spec.Dial,
		Port:        spec.Port,
		Domain:      spec.Path, // unused for tls; the SNI stays the address
		Fingerprint: spec.Fingerprint,
		Insecure:    spec.InsecureSkipVerify,
		Token:       e.cfg.Token,
		Timeout:     10 * time.Second,
	})
	if err != nil {
		return err
	}
	m := mux.New(ch, false, mux.Options{
		StreamWindow:    int64(e.cfg.StreamWindow),
		MaxStreamWindow: int64(e.cfg.MaxStreamWindow),
		Chunk:           e.cfg.Chunk,
		OnOpen:          e.handleOpen,
	})
	e.mu.Lock()
	e.tunnels = append(e.tunnels, m)
	e.peers[m] = ch.Peer()
	e.started[m] = time.Now()
	e.mu.Unlock()
	slog.Info("tunnel established with the relay", "peer", ch.Peer())
	m.Ctrl(map[string]any{"kind": "hello", "name": e.cfg.Name, "uptime": time.Since(e.start).Seconds()})
	go m.Keepalive(time.Duration(e.cfg.Keepalive) * time.Second)
	err = m.Run()
	e.mu.Lock()
	for i, other := range e.tunnels {
		if other == m {
			e.tunnels = append(e.tunnels[:i], e.tunnels[i+1:]...)
			break
		}
	}
	delete(e.peers, m)
	delete(e.started, m)
	e.mu.Unlock()
	return err
}

// Tunnels returns the live tunnel connections.
func (e *Exit) Tunnels() []*mux.Mux {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]*mux.Mux(nil), e.tunnels...)
}

// Status is the snapshot the web panel and `simurgh status` show: same field
// names as the Python engine so one panel serves both.
func (e *Exit) Status() map[string]any {
	e.mu.Lock()
	tunnels := append([]*mux.Mux(nil), e.tunnels...)
	peers := make(map[*mux.Mux]string, len(tunnels))
	started := make(map[*mux.Mux]time.Time, len(tunnels))
	for _, m := range tunnels {
		peers[m] = e.peers[m]
		started[m] = e.started[m]
	}
	listening := append([]string(nil), e.endpoint...)
	e.mu.Unlock()

	var in, out int64
	streams := 0
	entries := make([]map[string]any, 0, len(tunnels))
	for _, m := range tunnels {
		in += m.Stats.BytesIn.Load()
		out += m.Stats.BytesOut.Load()
		streams += m.StreamCount()
		up := 0.0
		if t := started[m]; !t.IsZero() {
			up = time.Since(t).Seconds()
		}
		entries = append(entries, map[string]any{
			"peer":    peers[m],
			"uptime":  up,
			"streams": m.StreamCount(),
			"rtt_ms":  float64(m.RTT().Microseconds()) / 1000.0,
		})
	}
	listenEntries := make([]map[string]any, 0, len(e.cfg.Listen))
	for _, spec := range e.cfg.Listen {
		ep := fmt.Sprintf("%s://%s:%d", spec.Carrier, spec.Host, spec.Port)
		if spec.Dial != "" {
			ep = fmt.Sprintf("%s://%s:%d (reverse: we dial)", spec.Carrier, spec.Dial, spec.Port)
		}
		listenEntries = append(listenEntries, map[string]any{
			"endpoint": ep, "carrier": spec.Carrier, "enabled": spec.Enabled,
		})
	}
	name := e.cfg.Name
	if name == "" {
		name = "exit"
	}
	return map[string]any{
		"role":           "exit",
		"name":           name,
		"engine":         "go",
		"listening":      listenEntries,
		"tunnels":        entries,
		"tunnel_count":   len(tunnels),
		"connections":    len(tunnels),
		"streams":        streams,
		"reach_out":      []string{},
		"targets":        []string{},
		"connected":      len(tunnels) > 0,
		"last_error":     e.lastError,
		"dial_errors":    e.dialErrors,
		"speedtest_port": e.cfg.SpeedtestPort,
		"push_ports":     e.cfg.PushPorts,
		"uptime":         time.Since(e.start).Seconds(),
		"endpoints":      listening,
		"stats": map[string]any{
			"totals": map[string]any{"in": in, "out": out, "sum": in + out, "conns": streams},
			"tunnel": map[string]any{"in_bytes": in, "out_bytes": out, "conns": streams},
		},
	}
}
