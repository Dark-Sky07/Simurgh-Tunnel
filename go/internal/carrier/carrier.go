// Package carrier is the disguise layer: the tunnel rides inside a real TLS
// connection (with a decoy website for anyone without the token), a websocket
// upgrade, or a plain TCP stream for testing.
package carrier

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/proto"
)

const readBuf = 256 << 10

// ---------------------------------------------------------------------------
// the framed channel

// ByteChannel is [4 byte length][frame] over any byte stream.
type ByteChannel struct {
	conn net.Conn
	rd   *bufio.Reader
	name string
	// scratch holds the payload of the frame being parsed; it is reused until
	// the next ReadFrame, which is exactly the contract the mux expects.
	scratch []byte
	wrote   bool // whether writes can use writev (plain TCP) or not
}

// NewByteChannel wraps a connection.
func NewByteChannel(conn net.Conn, name string) *ByteChannel {
	_, isTLS := conn.(*tls.Conn)
	return &ByteChannel{conn: conn, rd: bufio.NewReaderSize(conn, readBuf), name: name, wrote: isTLS}
}

// Name is the carrier name.
func (c *ByteChannel) Name() string { return c.name }

// Peer is the remote address.
func (c *ByteChannel) Peer() string {
	if c.conn == nil {
		return ""
	}
	return c.conn.RemoteAddr().String()
}

// ReadFrame parses the next frame, batching whatever is already buffered so a
// busy tunnel pays one syscall per burst instead of one per frame.
// The wire layout is [4 byte length][type][sid][payload], where the length
// covers the 5 byte frame header plus the payload.
func (c *ByteChannel) ReadFrame() (byte, uint32, []byte, error) {
	var lenBuf [4]byte
	if _, err := io.ReadFull(c.rd, lenBuf[:]); err != nil {
		return 0, 0, nil, err
	}
	n := int(binary.BigEndian.Uint32(lenBuf[:]))
	if n < proto.HeaderLen || n > proto.MaxFrame {
		return 0, 0, nil, fmt.Errorf("bad frame length %d", n)
	}
	if cap(c.scratch) < n {
		c.scratch = make([]byte, n)
	}
	frame := c.scratch[:n]
	if _, err := io.ReadFull(c.rd, frame); err != nil {
		return 0, 0, nil, err
	}
	return frame[0], binary.BigEndian.Uint32(frame[1:proto.HeaderLen]), frame[proto.HeaderLen:], nil
}

// WriteFrame writes a complete frame.
func (c *ByteChannel) WriteFrame(frame []byte) error {
	var lenBuf [4]byte
	binary.BigEndian.PutUint32(lenBuf[:], uint32(len(frame)))
	if _, isTLS := c.conn.(*tls.Conn); !isTLS {
		bufs := net.Buffers{lenBuf[:], frame}
		_, err := bufs.WriteTo(c.conn)
		return err
	}
	// TLS has no writev: one buffer, one record
	body := make([]byte, 4+len(frame))
	copy(body, lenBuf[:])
	copy(body[4:], frame)
	_, err := c.conn.Write(body)
	return err
}

// WriteData writes a DATA frame without copying the payload into the header.
func (c *ByteChannel) WriteData(hdr, payload []byte) error {
	n := len(hdr) + len(payload)
	var lenBuf [4]byte
	binary.BigEndian.PutUint32(lenBuf[:], uint32(n))
	if _, isTLS := c.conn.(*tls.Conn); !isTLS {
		bufs := net.Buffers{lenBuf[:], hdr, payload}
		_, err := bufs.WriteTo(c.conn)
		return err
	}
	body := make([]byte, 4+n)
	copy(body, lenBuf[:])
	copy(body[4:], hdr)
	copy(body[4+len(hdr):], payload)
	_, err := c.conn.Write(body)
	return err
}

// Close closes the connection.
func (c *ByteChannel) Close() error { return c.conn.Close() }

// SetDeadline forwards to the socket.
func (c *ByteChannel) SetDeadline(t time.Time) error { return c.conn.SetDeadline(t) }

// ---------------------------------------------------------------------------
// certificates

// CertPaths are the files the engine expects in <home>/cert.
func CertPaths(dir string) (string, string) {
	return filepath.Join(dir, "cert.pem"), filepath.Join(dir, "key.pem")
}

// EnsureCert loads the certificate in dir, generating a self-signed one the
// first time (exactly like the Python engine does).
func EnsureCert(dir, cn string) (tls.Certificate, error) {
	certPath, keyPath := CertPaths(dir)
	if _, err := os.Stat(certPath); err == nil {
		if cert, err := tls.LoadX509KeyPair(certPath, keyPath); err == nil {
			return cert, nil
		}
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return tls.Certificate{}, err
	}
	if cn == "" {
		cn = "simurgh.local"
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return tls.Certificate{}, err
	}
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: cn, Organization: []string{"Simurgh"}},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().AddDate(10, 0, 0),
		KeyUsage:     x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		DNSNames:     []string{cn},
		BasicConstraintsValid: true,
	}
	if ip := net.ParseIP(cn); ip != nil {
		tmpl.IPAddresses = []net.IP{ip}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, err
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return tls.Certificate{}, err
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	if err := os.WriteFile(certPath, certPEM, 0o644); err != nil {
		return tls.Certificate{}, err
	}
	if err := os.WriteFile(keyPath, keyPEM, 0o600); err != nil {
		return tls.Certificate{}, err
	}
	slog.Info("generated a self-signed certificate", "cn", cn, "cert", certPath)
	return tls.X509KeyPair(certPEM, keyPEM)
}

// LoadOrCreate returns the certificate for an endpoint: explicit files first,
// otherwise a self-signed one in dir (generated once, like the Python engine).
func LoadOrCreate(dir, cn, certFile, keyFile string) (tls.Certificate, error) {
	if certFile != "" && keyFile != "" {
		return tls.LoadX509KeyPair(certFile, keyFile)
	}
	return EnsureCert(dir, cn)
}

// Fingerprint is the SHA-256 of the certificate's DER, the value users pin.
func Fingerprint(cert tls.Certificate) (string, error) {
	if len(cert.Certificate) == 0 {
		return "", errors.New("empty certificate")
	}
	sum := sha256.Sum256(cert.Certificate[0])
	return hex.EncodeToString(sum[:]), nil
}

// ---------------------------------------------------------------------------
// server side

// Server serves one carrier: it authenticates and hands over a channel, or
// shows the decoy to whoever fails.
type Server struct {
	Carrier  string
	Token    string
	Fallback string // "decoy" or "close"
	Cert     tls.Certificate
	replay   *proto.ReplayCache
}

// NewServer builds a server carrier.
func NewServer(name, token string, cert tls.Certificate, fallback string) *Server {
	if fallback == "" {
		fallback = "decoy"
	}
	return &Server{Carrier: strings.ToLower(name), Token: token, Fallback: fallback,
		Cert: cert, replay: proto.NewReplayCache()}
}

// Serve handles one accepted connection. onChannel is called only when the
// peer proved it knows the token.
func (s *Server) Serve(conn net.Conn, onChannel func(mux.Channel)) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("carrier panic", "err", r)
			conn.Close()
		}
	}()
	switch s.Carrier {
	case "plain":
		s.servePlain(conn, onChannel)
	case "tls":
		s.serveTLS(conn, onChannel)
	case "wss":
		s.serveWSS(conn, onChannel)
	default:
		conn.Close()
	}
}

func (s *Server) servePlain(conn net.Conn, onChannel func(mux.Channel)) {
	_ = conn.SetReadDeadline(time.Now().Add(15 * time.Second))
	hdr := make([]byte, 25)
	if _, err := io.ReadFull(conn, hdr); err != nil {
		conn.Close()
		return
	}
	ts, ok := proto.CheckClientHeader(hdr, s.Token)
	if !ok || !s.replay.Add(hdr[9:25]) {
		conn.Close()
		return
	}
	_ = conn.SetReadDeadline(time.Time{})
	if _, err := conn.Write(proto.ServerHeader(s.Token, ts, time.Now().Unix())); err != nil {
		conn.Close()
		return
	}
	onChannel(NewByteChannel(conn, "plain"))
}

func (s *Server) serveTLS(conn net.Conn, onChannel func(mux.Channel)) {
	cfg := &tls.Config{
		Certificates: []tls.Certificate{s.Cert},
		NextProtos:   []string{"h2", "http/1.1"},
		MinVersion:   tls.VersionTLS12,
	}
	tlsConn := tls.Server(conn, cfg)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := tlsConn.HandshakeContext(ctx); err != nil {
		conn.Close()
		return
	}
	_ = tlsConn.SetReadDeadline(time.Now().Add(15 * time.Second))
	br := bufio.NewReaderSize(tlsConn, readBuf)
	hdr := make([]byte, 25)
	if _, err := io.ReadFull(br, hdr); err != nil {
		conn.Close()
		return
	}
	ts, ok := proto.CheckClientHeader(hdr, s.Token)
	if !ok || !s.replay.Add(hdr[9:25]) {
		// a real website would answer here; so do we
		_ = tlsConn.SetReadDeadline(time.Now().Add(10 * time.Second))
		if s.Fallback == "close" {
			conn.Close()
			return
		}
		ServeDecoy(tlsConn, br)
		conn.Close()
		return
	}
	_ = tlsConn.SetReadDeadline(time.Time{})
	if _, err := tlsConn.Write(proto.ServerHeader(s.Token, ts, time.Now().Unix())); err != nil {
		conn.Close()
		return
	}
	ch := NewByteChannel(tlsConn, "tls")
	ch.rd = br
	onChannel(ch)
}

func (s *Server) serveWSS(conn net.Conn, onChannel func(mux.Channel)) {
	cfg := &tls.Config{
		Certificates: []tls.Certificate{s.Cert},
		NextProtos:   []string{"http/1.1"},
		MinVersion:   tls.VersionTLS12,
	}
	tlsConn := tls.Server(conn, cfg)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := tlsConn.HandshakeContext(ctx); err != nil {
		conn.Close()
		return
	}
	br := bufio.NewReaderSize(tlsConn, readBuf)
	_ = tlsConn.SetReadDeadline(time.Now().Add(15 * time.Second))
	key, ok := wsAccept(br, tlsConn)
	if !ok {
		if s.Fallback != "close" {
			_ = tlsConn.SetReadDeadline(time.Now().Add(10 * time.Second))
			ServeDecoy(tlsConn, br)
		}
		conn.Close()
		return
	}
	_ = key
	ws := newWSChannel(tlsConn, br, false, "wss")
	// the first websocket frame carries the 25 byte authentication header
	frame, err := ws.readMessage()
	if err != nil {
		conn.Close()
		return
	}
	ts, ok := proto.CheckClientHeader(frame, s.Token)
	if !ok || !s.replay.Add(frame[9:25]) {
		ws.closeWithCode(1008)
		conn.Close()
		return
	}
	if err := ws.writeMessage(proto.ServerHeader(s.Token, ts, time.Now().Unix())); err != nil {
		conn.Close()
		return
	}
	_ = tlsConn.SetReadDeadline(time.Time{})
	ch := NewByteChannel(tlsConn, "wss")
	ch.rd = nil // the websocket layer owns the reader now
	onChannel(newWSAdapter(ws))
}

// ---------------------------------------------------------------------------
// client side

// ClientOptions describe how to reach the other end.
type ClientOptions struct {
	Carrier     string
	Address     string
	Port        int
	Domain      string   // SNI
	Path        string   // websocket path
	Fingerprint string   // pinned SHA-256 of the certificate
	Insecure    bool
	Timeout     time.Duration
	Token       string
}

// Dial opens an authenticated channel to the other side.
func Dial(opt ClientOptions) (mux.Channel, error) {
	if opt.Timeout <= 0 {
		opt.Timeout = 10 * time.Second
	}
	if opt.Path == "" {
		opt.Path = "/ws"
	}
	switch strings.ToLower(opt.Carrier) {
	case "plain":
		conn, err := dialTCP(opt.Address, opt.Port, opt.Timeout)
		if err != nil {
			return nil, err
		}
		ts := proto.ClientTimestamp()
		if _, err := conn.Write(proto.ClientHeader(opt.Token, ts)); err != nil {
			conn.Close()
			return nil, err
		}
		_ = conn.SetReadDeadline(time.Now().Add(opt.Timeout))
		resp := make([]byte, 25)
		if _, err := io.ReadFull(conn, resp); err != nil {
			conn.Close()
			return nil, err
		}
		if !proto.CheckServerHeader(resp, opt.Token, ts) {
			conn.Close()
			return nil, errors.New("server failed authentication (wrong token?)")
		}
		_ = conn.SetReadDeadline(time.Time{})
		return NewByteChannel(conn, "plain"), nil

	case "tls", "wss":
		sni := opt.Domain
		if sni == "" {
			sni = opt.Address
		}
		cfg := &tls.Config{
			ServerName:         sni,
			MinVersion:         tls.VersionTLS12,
			InsecureSkipVerify: opt.Insecure || opt.Fingerprint != "",
		}
		if opt.Fingerprint != "" {
			want := strings.ToLower(strings.ReplaceAll(strings.ReplaceAll(opt.Fingerprint, ":", ""), " ", ""))
			cfg.VerifyPeerCertificate = func(rawCerts [][]byte, _ [][]*x509.Certificate) error {
				if len(rawCerts) == 0 {
					return errors.New("server presented no certificate")
				}
				sum := sha256.Sum256(rawCerts[0])
				if hex.EncodeToString(sum[:]) != want {
					return fmt.Errorf("certificate fingerprint mismatch (got %s…, want %s…)",
						hex.EncodeToString(sum[:])[:16], want[:16])
				}
				return nil
			}
		}
		conn, err := dialTCP(opt.Address, opt.Port, opt.Timeout)
		if err != nil {
			return nil, err
		}
		tlsConn := tls.Client(conn, cfg)
		ctx, cancel := context.WithTimeout(context.Background(), opt.Timeout)
		defer cancel()
		if err := tlsConn.HandshakeContext(ctx); err != nil {
			conn.Close()
			return nil, err
		}
		if strings.ToLower(opt.Carrier) == "tls" {
			ts := proto.ClientTimestamp()
			if _, err := tlsConn.Write(proto.ClientHeader(opt.Token, ts)); err != nil {
				conn.Close()
				return nil, err
			}
			_ = tlsConn.SetReadDeadline(time.Now().Add(opt.Timeout))
			resp := make([]byte, 25)
			if _, err := io.ReadFull(tlsConn, resp); err != nil {
				conn.Close()
				return nil, err
			}
			if !proto.CheckServerHeader(resp, opt.Token, ts) {
				tlsConn.Close()
				return nil, errors.New("server failed authentication (wrong token?)")
			}
			_ = tlsConn.SetReadDeadline(time.Time{})
			return NewByteChannel(tlsConn, "tls"), nil
		}
		// websocket: upgrade, then the auth header as the first frame
		br := bufio.NewReaderSize(tlsConn, readBuf)
		if err := wsUpgrade(br, tlsConn, sni, opt.Path); err != nil {
			tlsConn.Close()
			return nil, err
		}
		ws := newWSChannel(tlsConn, br, true, "wss")
		ts := proto.ClientTimestamp()
		if err := ws.writeMessage(proto.ClientHeader(opt.Token, ts)); err != nil {
			tlsConn.Close()
			return nil, err
		}
		resp, err := ws.readMessage()
		if err != nil {
			tlsConn.Close()
			return nil, err
		}
		if !proto.CheckServerHeader(resp, opt.Token, ts) {
			tlsConn.Close()
			return nil, errors.New("server failed authentication (wrong token?)")
		}
		return newWSAdapter(ws), nil
	}
	return nil, fmt.Errorf("unknown carrier: %q", opt.Carrier)
}

func dialTCP(host string, port int, timeout time.Duration) (net.Conn, error) {
	conn, err := net.DialTimeout("tcp", net.JoinHostPort(host, fmt.Sprint(port)), timeout)
	if err != nil {
		return nil, err
	}
	if tc, ok := conn.(*net.TCPConn); ok {
		tc.SetNoDelay(true)
	}
	return conn, nil
}

// ---------------------------------------------------------------------------
// decoy website

const decoyPage = `<!DOCTYPE html>
<html>
<head>
<title>Welcome to nginx!</title>
<style>
html { color-scheme: light dark; }
body { width: 35em; margin: 0 auto; font-family: Tahoma, Verdana, Arial, sans-serif; }
</style>
</head>
<body>
<h1>Welcome to nginx!</h1>
<p>If you see this page, the nginx web server is successfully installed and
working. Further configuration is required.</p>

<p>For online documentation and support please refer to
<a href="http://nginx.org/">nginx.org</a>.<br/>
Commercial support is available at
<a href="http://nginx.com/">nginx.com</a>.</p>

<p><em>Thank you for using nginx.</em></p>
</body>
</html>
`

const notFoundPage = `<html>
<head><title>404 Not Found</title></head>
<body>
<center><h1>404 Not Found</h1></center>
<hr><center>nginx</center>
</body>
</html>
`

// ServeDecoy answers an HTTP request over the given stream with a boring
// nginx page, so a prober sees a web server and nothing else.
func ServeDecoy(conn io.Writer, br *bufio.Reader) {
	line, err := br.ReadString('\n')
	if err != nil {
		return
	}
	// drain the rest of the head
	for {
		l, err := br.ReadString('\n')
		if err != nil {
			return
		}
		if l == "\r\n" || l == "\n" {
			break
		}
	}
	path := "/"
	fields := strings.Fields(line)
	if len(fields) >= 2 {
		path = fields[1]
	}
	body := decoyPage
	status := "200 OK"
	if path != "/" && path != "/index.html" && !strings.HasPrefix(path, "/?") {
		body, status = notFoundPage, "404 Not Found"
	}
	now := time.Now().UTC().Format(time.RFC1123)
	resp := fmt.Sprintf("HTTP/1.1 %s\r\nServer: nginx/1.24.0\r\nDate: %s\r\n"+
		"Content-Type: text/html\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s",
		status, now, len(body), body)
	_, _ = conn.Write([]byte(resp))
}

// ---------------------------------------------------------------------------
// minimal websocket (RFC 6455) for the wss carrier

type wsChannel struct {
	conn   net.Conn
	br     *bufio.Reader
	mask   bool // true on the client side
	name   string
	buf    []byte
	closed bool
}

func newWSChannel(conn net.Conn, br *bufio.Reader, mask bool, name string) *wsChannel {
	return &wsChannel{conn: conn, br: br, mask: mask, name: name}
}

func wsUpgrade(br *bufio.Reader, conn net.Conn, host, path string) error {
	keyBytes := make([]byte, 16)
	if _, err := rand.Read(keyBytes); err != nil {
		return err
	}
	key := base64.StdEncoding.EncodeToString(keyBytes)
	req := fmt.Sprintf("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"+
		"Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n",
		path, host, key)
	if _, err := conn.Write([]byte(req)); err != nil {
		return err
	}
	status, headers, err := readHead(br)
	if err != nil {
		return err
	}
	if !strings.Contains(status, "101") {
		return fmt.Errorf("websocket upgrade refused: %s", status)
	}
	expect := base64.StdEncoding.EncodeToString(sha256sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")))
	if got := headers["sec-websocket-accept"]; got != "" && got != expect {
		return errors.New("bad websocket accept key")
	}
	return nil
}

// wsAccept reads the client's upgrade request and answers 101.
func wsAccept(br *bufio.Reader, conn net.Conn) (string, bool) {
	status, headers, err := readHead(br)
	if err != nil {
		return "", false
	}
	key := headers["sec-websocket-key"]
	if key == "" || !strings.Contains(strings.ToLower(status), "get ") {
		return "", false
	}
	accept := base64.StdEncoding.EncodeToString(sha256sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")))
	resp := "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n" +
		"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n"
	if _, err := conn.Write([]byte(resp)); err != nil {
		return "", false
	}
	return key, true
}

func sha256sum(b []byte) []byte {
	sum := sha256.Sum256(b)
	return sum[:]
}

func readHead(br *bufio.Reader) (string, map[string]string, error) {
	line, err := br.ReadString('\n')
	if err != nil {
		return "", nil, err
	}
	headers := map[string]string{}
	for {
		l, err := br.ReadString('\n')
		if err != nil {
			return "", nil, err
		}
		l = strings.TrimRight(l, "\r\n")
		if l == "" {
			break
		}
		if i := strings.Index(l, ":"); i > 0 {
			headers[strings.ToLower(strings.TrimSpace(l[:i]))] = strings.TrimSpace(l[i+1:])
		}
	}
	return line, headers, nil
}

// writeMessage sends one binary websocket frame.
func (w *wsChannel) writeMessage(payload []byte) error {
	var head []byte
	n := len(payload)
	switch {
	case n < 126:
		head = []byte{0x82, byte(n)}
	case n < 65536:
		head = []byte{0x82, 126, byte(n >> 8), byte(n)}
	default:
		head = []byte{0x82, 127, 0, 0, 0, 0, byte(n >> 24), byte(n >> 16), byte(n >> 8), byte(n)}
	}
	if w.mask {
		head[1] |= 0x80
		var key [4]byte
		if _, err := rand.Read(key[:]); err != nil {
			return err
		}
		head = append(head, key[:]...)
		body := make([]byte, n)
		for i := 0; i < n; i++ {
			body[i] = payload[i] ^ key[i%4]
		}
		if _, err := w.conn.Write(append(head, body...)); err != nil {
			return err
		}
		return nil
	}
	// then the masked body (or the payload as-is) in one write
	buf := append(head, payload...)
	_, err := w.conn.Write(buf)
	return err
}

// readMessage returns the next binary message, skipping control frames.
func (w *wsChannel) readMessage() ([]byte, error) {
	for {
		var h [2]byte
		if _, err := io.ReadFull(w.br, h[:]); err != nil {
			return nil, err
		}
		opcode := h[0] & 0x0f
		masked := h[1]&0x80 != 0
		length := int(h[1] & 0x7f)
		switch length {
		case 126:
			var b [2]byte
			if _, err := io.ReadFull(w.br, b[:]); err != nil {
				return nil, err
			}
			length = int(binary.BigEndian.Uint16(b[:]))
		case 127:
			var b [8]byte
			if _, err := io.ReadFull(w.br, b[:]); err != nil {
				return nil, err
			}
			length = int(binary.BigEndian.Uint64(b[:]))
		}
		if length > proto.MaxFrame+proto.HeaderLen {
			return nil, errors.New("websocket frame too large")
		}
		var key [4]byte
		if masked {
			if _, err := io.ReadFull(w.br, key[:]); err != nil {
				return nil, err
			}
		}
		payload := make([]byte, length)
		if _, err := io.ReadFull(w.br, payload); err != nil {
			return nil, err
		}
		if masked {
			for i := range payload {
				payload[i] ^= key[i%4]
			}
		}
		switch opcode {
		case 0x2, 0x1, 0x0: // binary / text / continuation
			return payload, nil
		case 0x8: // close
			_ = w.closeWithCode(1000)
			return nil, io.EOF
		case 0x9: // ping -> pong
			_ = w.writeControl(0xA, payload)
		case 0xA: // pong
		}
	}
}

func (w *wsChannel) writeControl(opcode byte, payload []byte) error {
	head := []byte{0x80 | opcode, byte(len(payload))}
	if w.mask {
		var key [4]byte
		if _, err := rand.Read(key[:]); err != nil {
			return err
		}
		head[1] |= 0x80
		head = append(head, key[:]...)
		body := make([]byte, len(payload))
		for i := range payload {
			body[i] = payload[i] ^ key[i%4]
		}
		_, err := w.conn.Write(append(head, body...))
		return err
	}
	_, err := w.conn.Write(append(head, payload...))
	return err
}

func (w *wsChannel) closeWithCode(code uint16) error {
	var b [2]byte
	binary.BigEndian.PutUint16(b[:], code)
	return w.writeControl(0x8, b[:])
}

// wsAdapter exposes a websocket as a mux Channel.
type wsAdapter struct {
	ws *wsChannel
}

func newWSAdapter(ws *wsChannel) *wsAdapter { return &wsAdapter{ws: ws} }

func (a *wsAdapter) Name() string { return a.ws.name }
func (a *wsAdapter) Peer() string { return a.ws.conn.RemoteAddr().String() }

func (a *wsAdapter) ReadFrame() (byte, uint32, []byte, error) {
	msg, err := a.ws.readMessage()
	if err != nil {
		return 0, 0, nil, err
	}
	if len(msg) < proto.HeaderLen {
		return 0, 0, nil, errors.New("short frame")
	}
	return msg[0], binary.BigEndian.Uint32(msg[1:proto.HeaderLen]), msg[proto.HeaderLen:], nil
}

func (a *wsAdapter) WriteFrame(frame []byte) error { return a.ws.writeMessage(frame) }

func (a *wsAdapter) WriteData(hdr, payload []byte) error {
	buf := make([]byte, 0, len(hdr)+len(payload))
	buf = append(buf, hdr...)
	buf = append(buf, payload...)
	return a.ws.writeMessage(buf)
}

func (a *wsAdapter) Close() error { return a.ws.conn.Close() }
