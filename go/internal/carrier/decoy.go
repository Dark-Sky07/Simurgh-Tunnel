package carrier

import (
	"bufio"
	"crypto/tls"
	"io"
	"net"
	"os"
	"strings"
	"time"
)

// The decoy: what an active prober sees when it knocks on the listener without
// the token. Three behaviours, mirroring the Python engine:
//
//	fallback = "decoy"           a boring, believable web page (default)
//	fallback = "site:host:port"  transparently proxy to a real website, so the
//	                             port really is a website
//	fallback = "close"           say nothing at all
//
// Consistency is the point: TLS, plain HTTP and garbage must all look like a
// quiet web server, so a filter cannot tell the tunnel apart from a website.

// DecoyPage is the deliberately boring default page: half the internet serves
// a variation of it, and blending in is the goal.
const DecoyPage = `<!DOCTYPE html>
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

const notFoundPage = `<!DOCTYPE html>
<html>
<head><title>404 Not Found</title></head>
<body>
<center><h1>404 Not Found</h1></center>
<hr><center>nginx</center>
</body>
</html>
`

// decoyOptions is what a listening endpoint needs to answer a probe.
type decoyOptions struct {
	fallback  string
	decoyFile string
}

// serveDecoy answers one unauthenticated connection like a quiet web server
// would. When the carrier was tls/wss the bytes travel inside the TLS session,
// so the decoy talks through tlsConn; conn is only there to close both layers.
func serveDecoy(conn net.Conn, tlsConn *tls.Conn, opt decoyOptions, initial []byte) {
	defer conn.Close()
	talk := conn
	if tlsConn != nil {
		talk = tlsConn
	}
	fallback := strings.TrimSpace(opt.fallback)
	if fallback == "" {
		fallback = "decoy"
	}
	switch {
	case strings.HasPrefix(fallback, "site:"):
		relayDecoySite(talk, strings.TrimPrefix(fallback, "site:"), initial)
		return
	case fallback == "close":
		return
	}

	// HTTP/2 is what browsers ask for first; a real nginx would either speak it
	// or tell the client to use HTTP/1.1. Saying "please use HTTP/1.1" is a
	// perfectly normal answer, and it keeps us honest about what we are.
	if tlsConn != nil && tlsConn.ConnectionState().NegotiatedProtocol == "h2" {
		sendHTTP2GoAway(talk)
		return
	}

	head := readHTTPHead(talk, initial, 8*time.Second, 16*1024)
	if len(head) == 0 {
		return
	}
	path := requestPath(head)
	if path == "" || !strings.Contains(string(head), "\r\n\r\n") {
		// Binary junk, or a head that never completed: a real web server
		// answers 400 and closes, and so do we.
		writeAll(talk, []byte(httpResponse("400 Bad Request", []byte(DecoyPage))))
		return
	}
	page := loadDecoyPage(opt.decoyFile)
	if path == "/" || path == "/index.html" || path == "/index.htm" {
		writeAll(talk, []byte(httpResponse("200 OK", page)))
		return
	}
	writeAll(talk, []byte(httpResponse("404 Not Found", []byte(notFoundPage))))
}

// relayDecoySite dials the configured website and splices the probe to it, so
// the port genuinely serves that site.
func relayDecoySite(conn net.Conn, target string, initial []byte) {
	up, err := net.DialTimeout("tcp", target, 8*time.Second)
	if err != nil {
		return
	}
	defer up.Close()
	if len(initial) > 0 {
		up.Write(initial)
	}
	done := make(chan struct{}, 2)
	go func() { io.Copy(up, conn); done <- struct{}{} }()
	go func() { io.Copy(conn, up); done <- struct{}{} }()
	<-done
}

// serveDecoyPlain is the same for a connection that never spoke TLS (carrier
// plain, or a plain HTTP probe against a TLS port).
func serveDecoyPlain(conn net.Conn, opt decoyOptions, initial []byte) {
	serveDecoy(conn, nil, opt, initial)
}

func loadDecoyPage(path string) []byte {
	if path != "" {
		if raw, err := os.ReadFile(path); err == nil && len(raw) > 0 {
			return raw
		}
	}
	return []byte(DecoyPage)
}

func readHTTPHead(conn net.Conn, initial []byte, timeout time.Duration, limit int) []byte {
	out := append([]byte(nil), initial...)
	if strings.Contains(string(out), "\r\n\r\n") {
		return out
	}
	conn.SetReadDeadline(time.Now().Add(timeout))
	defer conn.SetReadDeadline(time.Time{})
	buf := make([]byte, 4096)
	for len(out) < limit && !strings.Contains(string(out), "\r\n\r\n") {
		n, err := conn.Read(buf)
		if n > 0 {
			out = append(out, buf[:n]...)
		}
		if err != nil {
			break
		}
	}
	return out
}

func requestPath(head []byte) string {
	line := string(head)
	if idx := strings.Index(line, "\r\n"); idx >= 0 {
		line = line[:idx]
	}
	parts := strings.Fields(line)
	if len(parts) < 2 {
		return ""
	}
	switch strings.ToUpper(parts[0]) {
	case "GET", "HEAD", "POST", "PUT", "OPTIONS", "DELETE", "PATCH":
		return parts[1]
	}
	return ""
}

func httpResponse(status string, body []byte) string {
	return "HTTP/1.1 " + status + "\r\n" +
		"Server: nginx\r\n" +
		"Date: " + time.Now().UTC().Format(time.RFC1123) + "\r\n" +
		"Content-Type: text/html\r\n" +
		"Content-Length: " + itoa(len(body)) + "\r\n" +
		"Connection: close\r\n" +
		"\r\n" + string(body)
}

// sendHTTP2GoAway answers an HTTP/2 client with the frames a real server sends
// when it would rather speak HTTP/1.1: SETTINGS, then GOAWAY(HTTP_1_1_REQUIRED).
func sendHTTP2GoAway(conn net.Conn) {
	// SETTINGS with no parameters: 9 byte frame header, type 4, flags 0
	settings := []byte{0, 0, 0, 4, 0, 0, 0, 0, 0}
	// GOAWAY: last stream id 0, error code 0x0d (HTTP_1_1_REQUIRED)
	goaway := []byte{0, 0, 8, 7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 13}
	writeAll(conn, append(settings, goaway...))
}

func writeAll(conn net.Conn, data []byte) {
	conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
	_, _ = conn.Write(data)
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}

// firstBytes peeks at what a client sent before it says anything: used to tell
// a TLS ClientHello (0x16 0x03) from a plain HTTP request.
func firstBytes(br *bufio.Reader) []byte {
	peek, err := br.Peek(3)
	if err != nil {
		return nil
	}
	return append([]byte(nil), peek...)
}
