// One authenticated Windows Remote command -> one official Studio MCP request.
// No listeners, plugins, npm packages, or additional firewall rules.
'use strict';
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const crypto = require('crypto');
const {spawn} = require('child_process');
const readline = require('readline');

const folder = path.join(process.env.LOCALAPPDATA, 'WindowsRemote', 'roblox');
fs.mkdirSync(folder, {recursive:true});
const MAX = 32 * 1024 * 1024;
let child, timer, finished = false, stderr = '';

function spool(response) {
  if (finished) return;
  finished = true;
  clearTimeout(timer);
  if (child) child.kill();
  const data = zlib.gzipSync(Buffer.from(JSON.stringify(response), 'utf8'));
  if (data.length > MAX) throw new Error('MCP response exceeds 32 MiB');
  const id = crypto.randomUUID().replaceAll('-', '');
  fs.writeFileSync(path.join(folder, id + '.reply.gz'), data);
  console.log(JSON.stringify({id, bytes:data.length, sha256:crypto.createHash('sha256').update(data).digest('hex')}));
}

function findExe() {
  const base = path.join(process.env.LOCALAPPDATA, 'Roblox');
  const bat = path.join(base, 'mcp.bat');
  if (fs.existsSync(bat)) {
    const content = fs.readFileSync(bat, 'utf8');
    const candidates = content.match(/"([^"\r\n]*StudioMCP\.exe)"/gi) || [];
    for (const quoted of candidates) {
      const exe = quoted.slice(1,-1);
      if (fs.existsSync(exe)) return exe;
    }
  }
  const versions = path.join(base, 'Versions');
  const candidates = fs.readdirSync(versions).map(v=>path.join(versions,v,'StudioMCP.exe'))
    .filter(p=>fs.existsSync(p)).sort((a,b)=>fs.statSync(b).mtimeMs-fs.statSync(a).mtimeMs);
  if (!candidates.length) throw new Error('StudioMCP.exe not found. Enable Studio as MCP server in Roblox Assistant settings.');
  return candidates[0];
}

async function main() {
  // Clear abandoned request/response files without touching other application data.
  for (const name of fs.readdirSync(folder)) {
    if (/^[a-f0-9]{32}\.(reply|request)\.gz$/.test(name)) {
      const p = path.join(folder,name);
      if (Date.now()-fs.statSync(p).mtimeMs > 3600000) fs.unlinkSync(p);
    }
  }
  let packed;
  if (process.argv[2] === '--file') {
    const id = process.argv[3];
    if (!/^[a-f0-9]{32}$/.test(id)) throw new Error('Invalid request ID');
    const p = path.join(folder,id+'.request.gz');
    packed = fs.readFileSync(p);
    fs.unlinkSync(p);
  } else {
    packed = Buffer.from(process.argv[2] || '', 'base64');
  }
  const request = JSON.parse(zlib.gunzipSync(packed, {maxOutputLength:MAX}).toString('utf8'));
  if (!request || typeof request.method !== 'string') throw new Error('Invalid MCP request');
  const hasId = Object.hasOwn(request,'id');
  const originalId = request.id;
  let discoveryAttempts = 0;
  const fail = message => spool({jsonrpc:'2.0', id:originalId ?? null, error:{code:-32000,message}});
  child = spawn(findExe(), [], {windowsHide:true, stdio:['pipe','pipe','pipe']});
  child.stderr.on('data',data=>{stderr=(stderr+data.toString()).slice(-2000)});
  child.on('error',error=>fail(error.message));
  child.on('exit',(code)=>{if(!finished)fail('Studio MCP exited before replying ('+code+'). '+stderr)});
  timer = setTimeout(()=>fail('Studio MCP timed out after 105 seconds. Use asynchronous generation and short wait_job_finished calls.'),105000);
  function send(m) {child.stdin.write(JSON.stringify(m)+'\n')}
  child.stdin.on('error',error=>{if(!finished)fail(error.message)});
  readline.createInterface({input:child.stdout, crlfDelay:Infinity}).on('line',line=>{
    let m; try {m=JSON.parse(line)} catch {return}
    if (m.id === 'bridge-init') {
      if(m.error){fail(JSON.stringify(m.error));return}
      if(request.method === 'initialize') {
        m.id = originalId;
        // This per-request bridge cannot deliver unsolicited notifications.
        m.result.capabilities = {tools:{listChanged:false}};
        m.result.serverInfo.name = 'RobloxStudioRemote';
        m.result.instructions = (m.result.instructions || '') + ' Connected to MaxrPC through Windows Remote. First call list_roblox_studios and target studio_id explicitly. Do not invoke subagent unless the user requests delegation. Use async generation and wait_job_finished timeout <= 90. Local paths refer to the remote Windows PC.';
        spool(m);return;
      }
      send({jsonrpc:'2.0',method:'notifications/initialized'});
      if (!hasId) {send(request); spool({notification:true});return}
      if (request.method === 'tools/call') {
        send({jsonrpc:'2.0',id:'bridge-discover',method:'tools/call',params:{name:'list_roblox_studios',arguments:{}}});
      } else {
        send({...request,id:'bridge-call'});
      }
    } else if (m.id === 'bridge-discover') {
      let studios = [];
      try {
        const text = m.result.content.find(c=>c.type==='text').text;
        studios = JSON.parse(text).studios || [];
      } catch {}
      const target = request.params?.arguments?.studio_id;
      const ready = target ? studios.some(s=>s.id===target) : studios.length > 0;
      if (!ready && !m.error && discoveryAttempts++ < 20) {
        setTimeout(()=>{if(!finished)send({jsonrpc:'2.0',id:'bridge-discover',method:'tools/call',params:{name:'list_roblox_studios',arguments:{}}})},500);
      } else if (request.params?.name === 'list_roblox_studios') {
        m.id = originalId; spool(m);
      } else if (!ready) {
        fail('Target Studio window is not connected. Enable Studio as MCP server, open the place, and call list_roblox_studios again.');
      } else {
        send({...request,id:'bridge-call'});
      }
    } else if (m.id === 'bridge-call') {
      m.id = originalId;
      spool(m);
    } else if (m.method && Object.hasOwn(m,'id')) {
      send({jsonrpc:'2.0',id:m.id,error:{code:-32601,message:'Client-initiated bridge does not support sampling or elicitation'}});
    }
  });
  send({jsonrpc:'2.0',id:'bridge-init',method:'initialize',params:{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'Codex-WindowsRemote',version:'1.0.0'}}});
}

main().catch(error=>spool({jsonrpc:'2.0',id:null,error:{code:-32000,message:error.message}}));
