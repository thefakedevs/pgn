// PGN transport adaptation of PasarGuard/node-serviced v0.0.4 (GPL-3.0).
package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func listenUnix(path string, gid int) (net.Listener, error) {
	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return nil, fmt.Errorf("refusing to replace non-socket %s", path)
		}
		conn, dialErr := net.DialTimeout("unix", path, time.Second)
		if dialErr == nil {
			conn.Close()
			return nil, fmt.Errorf("socket is already in use: %s", path)
		}
		if !errors.Is(dialErr, syscall.ECONNREFUSED) {
			return nil, fmt.Errorf("cannot inspect existing socket: %w", dialErr)
		}
		if err := os.Remove(path); err != nil {
			return nil, err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	listener, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	if err = os.Chown(path, os.Getuid(), gid); err == nil {
		err = os.Chmod(path, 0660)
	}
	if err != nil {
		listener.Close()
		return nil, err
	}
	return listener, nil
}

func routes(s *server) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", s.handleRoot)
	mux.HandleFunc("POST /node/update", s.handleUpdate)
	mux.HandleFunc("POST /node/core_update", s.handleCoreUpdate)
	mux.HandleFunc("POST /node/geofiles", s.handleGeofiles)
	mux.HandleFunc("POST /node/hard_reset", s.handleHardReset)
	return mux
}

func main() {
	cfg, err := loadConfig()
	if err != nil {
		logf("%v", err)
		os.Exit(1)
	}
	listener, err := listenUnix(cfg.SocketPath, cfg.SocketGID)
	if err != nil {
		logf("%v", err)
		os.Exit(1)
	}
	defer listener.Close()
	httpServer := &http.Server{
		Handler:           routes(&server{cfg: cfg}),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}
	signals, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-signals.Done()
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		httpServer.Shutdown(ctx)
	}()
	logf("node-serviced listening on unix:%s (no TCP listener)", cfg.SocketPath)
	if err := httpServer.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logf("server error: %v", err)
		os.Exit(1)
	}
}
