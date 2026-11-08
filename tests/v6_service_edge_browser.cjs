const fs = require('fs'), {spawn, execFileSync} = require('child_process'), WebSocket = require('ws');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const chrome = spawn('chromium', ['--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
  '--disable-background-networking', '--disable-component-update', '--no-first-run', '--no-proxy-server',
  '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1', '--remote-debugging-port=0',
  '--user-data-dir=' + config.directory + '/chrome', 'about:blank'], {stdio:['ignore','ignore','pipe']});
let ws, stderr = '';
(async () => {
  const address = await new Promise((resolve,reject) => {
    chrome.stderr.on('data', chunk => { stderr += chunk; const m=stderr.match(/DevTools listening on (ws:\/\/\S+)/); if(m)resolve(m[1]); });
    chrome.once('exit', code => reject(Error('Chrome '+code+' '+stderr.slice(-512))));
    setTimeout(() => reject(Error('Chrome timeout')),10000).unref();
  });
  ws = new WebSocket(address); await new Promise((r,j) => {ws.once('open',r);ws.once('error',j);});
  let seq=0; const pending=new Map(), requests=[], responses=[], origins=new Map();
  ws.on('message', raw => {
    const m=JSON.parse(raw);
    if(m.id) {const p=pending.get(m.id);pending.delete(m.id);if(p)m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}
    if(m.method==='Network.requestWillBeSent') {
      requests.push({id:m.params.requestId,...m.params.request});
      if(m.params.redirectResponse)responses.push(m.params.redirectResponse);
    }
    if(m.method==='Network.responseReceived')responses.push({...m.params.response,requestId:m.params.requestId});
    if(m.method==='Network.requestWillBeSentExtraInfo' && m.params.headers.Origin!==undefined)origins.set(m.params.requestId,m.params.headers.Origin);
  });
  const call=(method,params={},sessionId) => new Promise((resolve,reject) => {const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));});
  const {targetId}=await call('Target.createTarget',{url:'about:blank'});
  const {sessionId:s}=await call('Target.attachToTarget',{targetId,flatten:true});
  await call('Page.enable',{},s);
  await call('Network.enable',{},s);
  const evaluate=async expression => {const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},s);if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  const wait=async expression => {for(let i=0;i<100;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,100));}throw Error('Condition timed out: '+expression+' '+await evaluate('document.body.innerText'));};
  if(['application-504','post-outage'].includes(config.mode)) {
    const path=config.mode==='application-504'?'/timeout':'/submit';
    await call('Page.navigate',{url:config.url},s);
    await wait('document.body?.innerText === "AnimeDB synthetic"');
    await evaluate(`(()=>{const form=document.createElement('form');form.method='POST';form.action=${JSON.stringify(path)};form.innerHTML='<input name="csrf" value="old-form-token">';document.body.append(form);return true;})()`);
    if(config.mode==='post-outage')execFileSync('docker',['--context','default','stop','-t','1',config.backend],{stdio:'ignore'});
    await evaluate('document.querySelector("form").requestSubmit();true');
    await wait('document.readyState === "complete" && location.pathname === '+JSON.stringify(path));
    if(config.mode==='post-outage') {
      await wait('!!document.getElementById("message")');
      execFileSync('docker',['--context','default','start',config.backend],{stdio:'ignore'});
      await wait('document.body.innerText === "AnimeDB synthetic"');
    }
    // Observe several original polling/reload cycles, not just the first POST.
    await new Promise(resolve=>setTimeout(resolve,2000));
    const posts=requests.filter(r=>r.method==='POST' && r.url===config.url+path);
    const visits=requests.filter(r=>r.url===config.url+path).map(r=>r.method);
    const response=responses.filter(r=>r.url===config.url+path).at(-1);
    const body=await evaluate('document.body.innerText');
    const info={scenario:config.mode,postCount:posts.length,visits,status:response?.status,body};
    console.log(JSON.stringify(info));
    if(posts.length!==1)throw Error('POST replay: '+JSON.stringify(info));
    if(config.mode==='application-504' && (response?.status!==504 || body!=='AnimeDB preflight timeout'))throw Error('Application 504 changed: '+JSON.stringify(info));
    if(config.mode==='application-504') {
      const original=await call('Network.getResponseBody',{requestId:response.requestId},s);
      if(original.base64Encoded || original.body!=='<html><body>AnimeDB preflight timeout</body></html>')throw Error('504 response bytes changed');
    }
    if(config.mode==='post-outage' && visits.join(',')!=='POST,GET')throw Error('Recovery must be a new GET: '+JSON.stringify(info));
    if(config.mode==='post-outage' && requests.filter(r=>r.url===config.url+path).at(-1).postData)throw Error('Old form body sent on recovery');
    return;
  }
  if(config.mode==='approval') {
    await call('Page.navigate',{url:config.url+config.path},s);
    await wait('!!document.getElementById("manifest-prepare-submit")');
    const negative=await evaluate(`fetch(location.pathname+'/prepare',{method:'POST',body:new URLSearchParams({csrf:'invalid',actor:'Edge operator'})}).then(r=>r.status)`);
    if(negative!==403)throw Error('Invalid CSRF accepted');
    await evaluate('document.querySelector("input[name=actor]").value="Edge operator";document.querySelectorAll("#manifest-warnings input[type=checkbox]").forEach(input=>input.click());true');
    await wait('!document.getElementById("manifest-prepare-submit").disabled');
    await evaluate('document.getElementById("manifest-prepare-submit").click();true');
    await wait('!!document.querySelector("form[action$=\'/approve\'] input[name=confirm]")');
    await evaluate('document.querySelector("form[action$=\'/approve\'] input[name=confirm]").click();document.querySelector("form[action$=\'/approve\'] button[type=submit]").click();true');
    await wait('document.body.innerText.includes("Schváleno") && document.readyState === "complete"');
    await new Promise(resolve=>setTimeout(resolve,1500));
    for(const ending of ['/prepare','/approve']) {
      const matches=responses.filter(r=>r.url===config.url+config.path+ending);
      const success=matches.filter(r=>r.status===303);
      if(success.length!==1)throw Error('PRG missing: '+JSON.stringify(matches));
    }
    const posts=requests.filter(r=>r.method==='POST' && r.url.startsWith(config.url+config.path));
    if(posts.length!==3 || posts.some(r=>origins.get(r.id)!==config.url))throw Error('Unexpected POST/Origin: '+JSON.stringify(posts));
    const headers=await evaluate('fetch(location.href).then(r=>({cache:r.headers.get("cache-control"),referrer:r.headers.get("referrer-policy")}))');
    if(headers.cache!=='no-store' || headers.referrer!=='same-origin')throw Error('Approval protection changed: '+JSON.stringify(headers));
    console.log(JSON.stringify({scenario:'approval',prepare:303,approve:303,invalidCsrf:negative,posts:posts.length,origins:'same-origin',headers}));
    return;
  }
  await call('Page.navigate',{url:config.url+'/reorganization'},s);
  await wait('document.getElementById("message")?.textContent.includes("Nemáme potvrzení")');
  const phases=['manifest','folders','rename','sqlite','cleanup','verification'];
  let data={version:1,run_id:'a'.repeat(32),active:true,current_phase:'rename',completed:128,rename_completed:129,total:3095,
    waiting_for_operator:false,outcome:'UNVERIFIED',last_confirmed:{phase:'sqlite',completed:128},
    phases:phases.map((id,i)=>({id,state:i<2?'PASS':i<4?'WORK':'WAIT'}))};
  const publish=(age=0) => {data.updated_at=Date.now()/1000-age;const p=config.public+'/temporary.json';fs.writeFileSync(p,JSON.stringify(data));fs.renameSync(p,config.public+'/progress.json');};
  publish();
  await wait('document.getElementById("result").textContent === "Reorganizace knihovny"');
  await wait('document.querySelector("[data-phase=sqlite]").innerText.includes("128 / 3095")');
  for(const [width,height,mobile] of [[1366,768,false],[1600,900,false],[1920,1080,false],[2560,1440,false],[820,1180,true],[1180,820,true]]) {
    await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile},s);
    await call('Emulation.setTouchEmulationEnabled',{enabled:mobile,maxTouchPoints:5},s);
    if(await evaluate('document.documentElement.scrollWidth>innerWidth+1'))throw Error('Horizontal overflow '+width);
    const shot=await call('Page.captureScreenshot',{format:'png'},s);
    fs.writeFileSync(config.directory+'/service-'+width+'x'+height+'.png',Buffer.from(shot.data,'base64'));
  }
  data.waiting_for_operator=true;data.phases[2].state=data.phases[3].state='PASS';publish();
  await wait('document.getElementById("message").textContent.includes("čeká na správce")');
  publish(3600);
  await wait('document.getElementById("result").textContent === "Stav neověřen"');
  if(await evaluate('document.getElementById("message").textContent.includes("Reorganizace probíhá")'))throw Error('Stale run shown live');
  data.active=false;data.outcome='STOP';data.phases[4].state='STOP';data.error={message:'Běh byl zastaven.'};publish();
  await wait('document.getElementById("result").textContent === "Poslední běh: STOP"');
  if(!await evaluate('document.querySelector("[data-phase=sqlite]").innerText.includes("128 / 3095")'))throw Error('Lost confirmed count');
  data.outcome='NORMAL';data.phases.forEach(row=>row.state='PASS');publish(86400);
  await wait('document.getElementById("result").textContent === "Poslední běh: NORMAL"');
  // A stale terminal record is explicitly a previous run, never a live execution.
  execFileSync('docker',['--context','default','start',config.backend],{stdio:'ignore'});
  await wait('document.body.innerText === "AnimeDB synthetic"');
  console.log(JSON.stringify({ordinaryOutage:true,liveCounts:true,cleanupWait:true,staleUnverified:true,stop:true,historicalNormal:true,returnToApp:true,viewports:6}));
})().catch(error => {console.error(error.stack);process.exitCode=1;}).finally(() => {if(ws)ws.close();chrome.kill('SIGTERM');});
