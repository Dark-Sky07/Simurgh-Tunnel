//go:build !(linux || darwin || freebsd || netbsd || openbsd || dragonfly)

package carrier

// Other platforms keep the conservative default window: the carrier asks for a
// large socket buffer but cannot read back what the kernel granted.
func getsockoptInt(fd, opt int) int { return 0 }

const soRCVBUF = 0x1002 // SOL_SOCKET / SO_RCVBUF on Windows
