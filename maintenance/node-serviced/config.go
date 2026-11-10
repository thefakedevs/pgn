// PGN transport adaptation of PasarGuard/node-serviced v0.0.4 (GPL-3.0).
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"

	"github.com/joho/godotenv"
)

const maxBodyBytes = int64(1 << 20)

type config struct {
	AppName    string
	APIKey     string
	SocketPath string
	SocketGID  int
}

func loadConfig() (config, error) {
	cfg := config{SocketGID: os.Getgid()}
	if envFile := os.Getenv("ENV_FILE"); envFile != "" {
		if err := godotenv.Overload(envFile); err != nil {
			return cfg, err
		}
	}
	cfg.AppName = os.Getenv("APP_NAME")
	cfg.APIKey = os.Getenv("API_KEY")
	cfg.SocketPath = os.Getenv("SOCKET_PATH")
	if cfg.APIKey == "" {
		return cfg, fmt.Errorf("API_KEY is required")
	}
	if !filepath.IsAbs(cfg.AppName) {
		return cfg, fmt.Errorf("APP_NAME must be an absolute executable path")
	}
	if !filepath.IsAbs(cfg.SocketPath) {
		return cfg, fmt.Errorf("SOCKET_PATH must be an absolute Unix socket path")
	}
	if gid := os.Getenv("SOCKET_GID"); gid != "" {
		n, err := strconv.Atoi(gid)
		if err != nil || n < 0 {
			return cfg, fmt.Errorf("invalid SOCKET_GID")
		}
		cfg.SocketGID = n
	}
	return cfg, nil
}
