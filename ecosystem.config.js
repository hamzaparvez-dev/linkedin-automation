/**
 * PM2 process file — run from repo root after ./setup.sh (venv at ./venv).
 *   pm2 start ecosystem.config.js
 *   pm2 logs leadgen-engagement
 *
 * Uses the venv interpreter so system Python is untouched.
 */
module.exports = {
  apps: [
    {
      name: 'leadgen-engagement',
      cwd: __dirname,
      script: 'main.py',
      interpreter: './venv/bin/python',
      args: '--engagement-only --multi-account-run',
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      min_uptime: '10s',
      restart_delay: 10_000,
      watch: false,
      time: true,
      env: {
        NODE_ENV: 'production',
      },
    },
  ],
}
