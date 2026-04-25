module.exports = {
  apps: [
    {
      name: 'algosoft-bot',
      cwd: './bot',
      script: 'python3',
      args: '-m uvicorn web.server:app --host 0.0.0.0 --port 5000 --workers 1',
      interpreter: 'none',

      // Restart policy
      autorestart: true,
      max_restarts: 20,
      min_uptime: '10s',
      restart_delay: 3000,
      exp_backoff_restart_delay: 100,

      // Environment
      env: {
        NODE_ENV: 'production',
        PYTHONPATH: '.',
      },

      // Logging — rotate via pm2-logrotate module
      output: './logs/pm2_out.log',
      error: './logs/pm2_err.log',
      merge_logs: true,
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',

      // Crash-alert watchdog — runs alongside the bot as a separate process
      // See scripts/health_watchdog.py for Telegram crash alerts
    },

    {
      name: 'algosoft-watchdog',
      cwd: '.',
      script: 'python3',
      args: 'scripts/health_watchdog.py',
      interpreter: 'none',
      autorestart: true,
      max_restarts: 50,
      min_uptime: '5s',
      restart_delay: 5000,
      output: './logs/watchdog_out.log',
      error: './logs/watchdog_err.log',
      merge_logs: true,
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    },
  ],
};
