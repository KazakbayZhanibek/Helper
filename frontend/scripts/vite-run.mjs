// Vite interprets # as a URL fragment in source paths. Keep the user's folder
// name intact and run through a stable, safe symlink when that character occurs.
import {createHash} from 'node:crypto';
import {existsSync, mkdirSync, readlinkSync, symlinkSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';

const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
let root = sourceRoot;
if (sourceRoot.includes('#')) {
  const key = createHash('sha256').update(sourceRoot).digest('hex').slice(0, 12);
  const runtime = path.join(tmpdir(), `master-experience-${key}`);
  mkdirSync(runtime, {recursive: true});
  root = path.join(runtime, 'web');
  if (!existsSync(root)) symlinkSync(sourceRoot, root, 'dir');
  else if (readlinkSync(root) !== sourceRoot) throw new Error('Unexpected Vite runtime link; choose a clean temporary directory.');
}
const [mode = 'dev', ...extra] = process.argv.slice(2);
const args = [path.join(root, 'node_modules/vite/bin/vite.js')];
if (mode !== 'dev') args.push(mode);
args.push(root, '--config', path.join(root, 'vite.config.ts'), '--configLoader', 'native');
if (mode !== 'build') args.push('--host', '127.0.0.1');
args.push(...extra);
const child = spawn(process.execPath, args, {cwd: root, stdio: 'inherit', env: process.env});
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
child.on('error', error => {console.error(error.message);process.exitCode = 1;});
child.on('exit', (code) => {process.exitCode = code ?? 1;});
