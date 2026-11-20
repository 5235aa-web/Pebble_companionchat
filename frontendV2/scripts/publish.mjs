#!/usr/bin/env node
/* Publishes the locally built installer to a GitHub Release and writes the
   updater feed (latest.json) next to it.

   `npm run release` chains build + publish. Run `npm run publish` alone to
   upload an installer that is already sitting in src-tauri/target/release/bundle/nsis.
*/

import { readFile, readdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const OWNER = '5235aa-web'
const REPO = 'Pebble_companionchat'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontend = path.resolve(here, '..')
const confPath = path.join(frontend, 'src-tauri', 'tauri.conf.json')
const bundleDir = path.join(frontend, 'src-tauri', 'target', 'release', 'bundle', 'nsis')

const token =
  process.env.GITHUB_TOKEN ||
  process.env.GH_TOKEN ||
  (await readFile(path.join(frontend, '.tauri-keys', 'github-token.txt'), 'utf8')
    .then(t => t.trim())
    .catch(() => ''))

if (!token) {
  console.error('No GitHub token: set GITHUB_TOKEN or save one at .tauri-keys/github-token.txt')
  process.exit(1)
}

const conf = JSON.parse(await readFile(confPath, 'utf8'))
const version = String(conf.version || '').trim()
if (!version) {
  console.error(`No version in ${confPath}.`)
  process.exit(1)
}
const tag = `v${version}`

if (!existsSync(bundleDir)) {
  console.error(`No build output at ${bundleDir}. Run "npm run tauri:build" first.`)
  process.exit(1)
}

const files = await readdir(bundleDir)
const installers = files.filter(f => f.endsWith('-setup.exe'))
const installer = installers.find(f => f.includes(`_${version}_`))
if (!installer) {
  console.error(
    `No ${version} installer in ${bundleDir}. Found: ${installers.join(', ') || 'none'}. Build version ${version} first.`,
  )
  process.exit(1)
}

const sigPath = path.join(bundleDir, `${installer}.sig`)
if (!existsSync(sigPath)) {
  console.error(
    `No ${installer}.sig. Set TAURI_SIGNING_PRIVATE_KEY before building, or the installer cannot be verified.`,
  )
  process.exit(1)
}

const signature = (await readFile(sigPath, 'utf8')).trim()
const installerBytes = await readFile(path.join(bundleDir, installer))

function notesFor(text, wanted) {
  const lines = text.split(/\r?\n/)
  const heading = new RegExp(`^##\\s+v?${wanted.replace(/\./g, '\\.')}\\s*$`)
  const start = lines.findIndex(line => heading.test(line.trim()))
  if (start === -1) return ''
  const body = []
  for (const line of lines.slice(start + 1)) {
    if (/^##\s/.test(line)) break
    body.push(line)
  }
  return body.join('\n').trim()
}

const changelogPath = path.join(frontend, '..', 'CHANGELOG.md')
const notesArg = process.argv.indexOf('--notes')
const explicitNotes = notesArg > -1 ? (process.argv[notesArg + 1] || '').trim() : ''
const notes =
  explicitNotes ||
  (existsSync(changelogPath) ? notesFor(await readFile(changelogPath, 'utf8'), version) : '')
if (!notes) {
  console.error(`No release notes for ${version}.`)
  console.error(`Add a "## ${version}" section to ${changelogPath}, or pass --notes "..."`)
  process.exit(1)
}

async function api(url, init = {}) {
  const res = await fetch(url, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'User-Agent': 'pebble-companionchat-release',
      ...(init.headers || {}),
    },
  })
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`${init.method || 'GET'} ${url} -> ${res.status} ${body.slice(0, 400)}`)
  }
  return res.json()
}

async function uploadAsset(releaseId, name, buffer, contentType) {
  const url = `https://uploads.github.com/repos/${OWNER}/${REPO}/releases/${releaseId}/assets?name=${encodeURIComponent(name)}`
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'User-Agent': 'pebble-companionchat-release',
      'Content-Type': contentType,
      'Content-Length': String(buffer.length),
    },
    body: buffer,
  })
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`upload ${name} -> ${res.status} ${body.slice(0, 400)}`)
  }
  return res.json()
}

console.log(`tag ${tag} · installer ${installer} (${(installerBytes.length / 1048576).toFixed(1)} MB)`)

const existing = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/releases/tags/${tag}`, {
  headers: {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'User-Agent': 'pebble-companionchat-release',
  },
})
if (existing.ok) {
  const body = await existing.json()
  console.error(`Release ${tag} already exists (${body.html_url}).`)
  console.error('Bump "version" in src-tauri/tauri.conf.json, rebuild, then run this again.')
  process.exit(1)
}

const release = await api(`https://api.github.com/repos/${OWNER}/${REPO}/releases`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    tag_name: tag,
    name: `Pebble Companion Chat ${version}`,
    body: notes,
    draft: false,
    prerelease: false,
  }),
})
console.log(`release created: ${release.html_url}`)

const asset = await uploadAsset(
  release.id,
  installer,
  installerBytes,
  'application/octet-stream',
)
console.log(`installer uploaded: ${asset.browser_download_url}`)

const feed = {
  version,
  notes,
  pub_date: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
  platforms: {
    'windows-x86_64': {
      signature,
      url: asset.browser_download_url,
    },
  },
}
const feedBytes = Buffer.from(JSON.stringify(feed, null, 2), 'utf8')
await uploadAsset(release.id, 'latest.json', feedBytes, 'application/json')
console.log('latest.json uploaded')
console.log('')
console.log('Done. Installed copies read:')
console.log(`  https://github.com/${OWNER}/${REPO}/releases/latest/download/latest.json`)
