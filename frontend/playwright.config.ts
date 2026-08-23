import { defineConfig } from '@playwright/test'
import { existsSync, readdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const port = 8012
const cachedHeadless = (() => {
  if (process.env.CI || process.platform !== 'darwin') return undefined
  const root = join(homedir(), 'Library', 'Caches', 'ms-playwright')
  if (!existsSync(root)) return undefined
  const installs = readdirSync(root).filter(name => name.startsWith('chromium_headless_shell-')).sort().reverse()
  return installs.map(name => join(root, name, 'chrome-headless-shell-mac-arm64', 'chrome-headless-shell')).find(existsSync)
})()
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || cachedHeadless

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  reporter: process.env.CI ? [['github'], ['list']] : [['list']],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    channel: executablePath || process.env.CI ? undefined : 'chrome',
    launchOptions: { executablePath },
    viewport: { width: 1280, height: 720 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `make -C .. demo PORT=${port}`,
    url: `http://127.0.0.1:${port}/api/health`,
    timeout: 60_000,
    reuseExistingServer: false,
    env: {
      ...process.env,
      FIELDFLOW_DB: `/tmp/fieldflow-playwright-${process.pid}.db`,
    },
  },
})
