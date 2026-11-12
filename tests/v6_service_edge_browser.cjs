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
  let seq=0; const pending=new Map(), requests=[], responses=[], origins=new Map(), heldPosts=[];
  ws.on('message', raw => {
    const m=JSON.parse(raw);
    if(m.id) {const p=pending.get(m.id);pending.delete(m.id);if(p)m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}
    if(m.method==='Network.requestWillBeSent') {
      requests.push({id:m.params.requestId,...m.params.request});
      if(m.params.redirectResponse)responses.push(m.params.redirectResponse);
    }
    if(m.method==='Network.responseReceived')responses.push({...m.params.response,requestId:m.params.requestId});
    if(m.method==='Network.requestWillBeSentExtraInfo' && m.params.headers.Origin!==undefined)origins.set(m.params.requestId,m.params.headers.Origin);
    if(m.method==='Fetch.requestPaused') {
      if(m.params.request.method==='POST')heldPosts.push(m.params.requestId);
      else call('Fetch.continueResponse',{requestId:m.params.requestId},m.sessionId).catch(error=>{console.error(error);process.exitCode=1;});
    }
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
      // Health alone does not prove a safe restart or a completed workflow.
      await new Promise(resolve=>setTimeout(resolve,1200));
      if(await evaluate('document.body.innerText === "AnimeDB synthetic"'))throw Error('Health-only recovery bypassed host readiness');
      const phases=['manifest','folders','rename','sqlite','cleanup','verification'];
      const ready={version:1,run_id:'a'.repeat(32),active:false,updated_at:Date.now()/1000,
        completed:0,rename_completed:0,total:0,outcome:'NORMAL',app_ready:true,source_sha256:'b'.repeat(64),
        result_url:'/reorganization/approval/'+'b'.repeat(64),phases:phases.map(id=>({id,state:'PASS'}))};
      fs.writeFileSync(config.public+'/progress.json',JSON.stringify(ready));
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
    if(config.mode==='post-outage' && visits.join(',')!=='POST')throw Error('Old form URL unexpectedly revisited: '+JSON.stringify(info));
    if(config.mode==='post-outage') {
      const returns=requests.filter(r=>r.url===config.url+'/reorganization/approval/'+'b'.repeat(64));
      if(returns.length!==1 || returns[0].method!=='GET' || returns[0].postData)throw Error('Canonical recovery must be one new GET');
    }
    return;
  }
  if(config.mode?.startsWith('cleanup-')) {
    const terminalMode=config.mode.startsWith('cleanup-terminal-'), latePost=config.mode.endsWith('-late-post');
    const countResponses=path=>responses.filter(row=>row.url===config.url+path).length;
    const waitForResponses=async(path,count)=>{
      for(let i=0;i<100;i++){if(countResponses(path)>=count)return;await new Promise(resolve=>setTimeout(resolve,50));}
      throw Error('Missing polling responses: '+path);
    };
    const outcome=config.mode.includes('-normal')?'NORMAL':'STOP';
    const phases=['manifest','folders','rename','sqlite','cleanup','verification'];
    const publish=(changes={})=>{
      const data={version:1,run_id:'a'.repeat(32),source_sha256:config.source,active:true,
        updated_at:Date.now()/1000,completed:1,rename_completed:1,total:1,outcome:'UNVERIFIED',
        stage:'cleanup',waiting_for_operator:true,app_ready:false,
        phases:phases.map(id=>({id,state:['cleanup','verification'].includes(id)?'WAIT':'PASS'})),...changes};
      fs.writeFileSync(config.public+'/temporary.json',JSON.stringify(data));
      fs.renameSync(config.public+'/temporary.json',config.public+'/progress.json');
    };
    if(terminalMode) {
      // Keep real polling/HTTP behavior, shortening only the browser's timer.
      await call('Page.addScriptToEvaluateOnNewDocument',{source:'const originalInterval=window.setInterval;window.setInterval=(fn,delay,...args)=>originalInterval(fn,delay===5000?250:delay,...args);'},s);
      publish();
      if(latePost)await call('Fetch.enable',{patterns:[{urlPattern:config.url+'/_service/control/cleanup',requestStage:'Response'}]},s);
    }
    await call('Page.navigate',{url:config.url+'/_service/workflow?source='+config.source},s);
    await wait('document.getElementById("cleanup-panel") && !document.getElementById("cleanup-panel").hidden');
    if(!await evaluate('document.querySelectorAll("#cleanup-candidates input:checked").length'))throw Error('Cleanup default selection missing');
    const detailResponse=responses.find(row=>row.url===config.url+'/_service/control/cleanup/proposal?sha='+config.proposal);
    if(!detailResponse)throw Error('No exact candidate response');
    const detailRaw=await call('Network.getResponseBody',{requestId:detailResponse.requestId},s);
    const detail=JSON.parse(detailRaw.base64Encoded?Buffer.from(detailRaw.body,'base64').toString():detailRaw.body);
    if(!await evaluate('!document.getElementById("cleanup-list").open && !document.getElementById("cleanup-selected").closest("details")'))throw Error('Cleanup disclosure/summary contract');
    if(['cleanup-delete','cleanup-two-browsers'].includes(config.mode)) {
      for(const [width,height,mobile] of [[1366,768,false],[1600,900,false],[1920,1080,false],[2560,1440,false],[820,1180,true],[1180,820,true]]) {
        await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile},s);
        await call('Emulation.setTouchEmulationEnabled',{enabled:mobile,maxTouchPoints:5},s);
        if(await evaluate('document.documentElement.scrollWidth>innerWidth+1'))throw Error('Cleanup horizontal overflow '+width);
        if(!await evaluate('!document.getElementById("cleanup-list").open'))throw Error('Disclosure unexpectedly open');
        if(mobile) {
          const point=await evaluate('(()=>{const n=document.querySelector("#cleanup-list summary");n.scrollIntoView({block:"center"});const r=n.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()');
          await call('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[point]},s);
          await call('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]},s);
        } else await evaluate('document.querySelector("#cleanup-list summary").click();true');
        await wait('document.getElementById("cleanup-list").open');
        if(mobile) {
          for(const id of ['cleanup-none','cleanup-all']) {
            const point=await evaluate(`(()=>{const n=document.getElementById(${JSON.stringify(id)});n.scrollIntoView({block:'center'});const r=n.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
            await call('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[point]},s);
            await call('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]},s);
            await wait('document.getElementById("cleanup-selected").textContent.includes('+JSON.stringify('Odstranit: '+(id==='cleanup-none'?0:detail.proposal.candidates.length))+')');
          }
        }
        await evaluate('document.querySelector("#cleanup-list summary").click();true');
        if(!await evaluate('!document.getElementById("cleanup-list").open && document.querySelectorAll("#cleanup-candidates input:checked").length>0'))throw Error('Collapse reset selection');
        const shot=await call('Page.captureScreenshot',{format:'png'},s);
        fs.writeFileSync(config.directory+'/cleanup-'+width+'x'+height+'.png',Buffer.from(shot.data,'base64'));
      }
    }
    let s2;
    if(config.mode==='cleanup-two-browsers') {
      const {browserContextId}=await call('Target.createBrowserContext');
      const target=await call('Target.createTarget',{url:'about:blank',browserContextId});
      s2=(await call('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
      await call('Page.enable',{},s2);await call('Network.enable',{},s2);
      await call('Page.navigate',{url:config.url+'/_service/workflow?source='+config.source},s2);
      for(let i=0;i<100;i++) {
        const view=await call('Runtime.evaluate',{expression:'!!document.getElementById("cleanup-panel")&&!document.getElementById("cleanup-panel").hidden',returnByValue:true},s2);
        if(view.result.value)break;
        if(i===99)throw Error('Second browser did not receive proposal');
        await new Promise(r=>setTimeout(r,100));
      }
    }
    await evaluate('document.getElementById("cleanup-actor").value="Edge cleanup operator";true');
    if(config.mode==='cleanup-delete') {
      await evaluate('document.getElementById("cleanup-sha").value='+JSON.stringify(config.proposal)+';document.getElementById("cleanup-confirm").checked=true;document.getElementById("cleanup-delete").click();true');
    } else {
      await evaluate('document.getElementById("cleanup-'+(config.mode==='cleanup-stop'?'stop':'preserve')+'").click();true');
    }
    if(latePost) {
      for(let i=0;i<100&&!heldPosts.length;i++)await new Promise(resolve=>setTimeout(resolve,20));
      if(heldPosts.length!==1)throw Error('Decision acknowledgement was not held');
    } else {
      await wait('document.getElementById("cleanup-feedback").textContent.includes('+JSON.stringify(config.mode==='cleanup-stop'?'Odmítnutí předáno':'Rozhodnutí předáno')+')');
    }
    if(terminalMode) {
      if(!latePost) {
        // Historical and foreign terminal records cannot settle this decision.
        publish({active:false,outcome,updated_at:Date.now()/1000-3600});
        await wait('document.getElementById("result").dataset.outcome === '+JSON.stringify(outcome));
        if(!await evaluate('document.getElementById("cleanup-feedback").textContent.includes("Čeká se na E7H")'))throw Error('Stale progress settled the cleanup decision');
        const seen=countResponses('/_service/progress.json');
        publish({active:false,outcome,source_sha256:'e'.repeat(64)});
        await waitForResponses('/_service/progress.json',seen+2);
        if(!await evaluate('document.getElementById("cleanup-feedback").textContent.includes("Čeká se na E7H")'))throw Error('Foreign progress settled the cleanup decision');
      }
      publish({active:false,outcome,stage:outcome==='STOP'?'stopped':'normal',waiting_for_operator:false,
        error:outcome==='STOP'?{message:'E7H fixture failure'}:null,
        phases:phases.map(id=>({id,state:outcome==='STOP'&&id==='cleanup'?'STOP':'PASS'}))});
      await wait('document.getElementById("cleanup-feedback").dataset.outcome === '+JSON.stringify(outcome)+' && !document.getElementById("cleanup-feedback").textContent.includes("Čeká se")');
      if(outcome==='STOP'&&!await evaluate('document.getElementById("cleanup-feedback").textContent.includes("automaticky neopakuje")'))throw Error('STOP lacks explicit no-retry feedback');
      if(latePost)await call('Fetch.continueResponse',{requestId:heldPosts[0]},s);
      // CONSUMED is returned by the real Unix control API on every later poll.
      const seen=countResponses('/_service/control/cleanup');
      await waitForResponses('/_service/control/cleanup',seen+3);
      const feedback=await evaluate('document.getElementById("cleanup-feedback").textContent');
      if(!await evaluate('document.getElementById("cleanup-feedback").dataset.outcome === '+JSON.stringify(outcome))||feedback.includes('Čeká se'))throw Error('Late acknowledgement/CONSUMED resurrected waiting: '+feedback);
      if(!latePost) {
        const progressSeen=countResponses('/_service/progress.json');
        await call('Page.reload',{},s);
        await waitForResponses('/_service/progress.json',progressSeen+2);
        await wait('document.getElementById("cleanup-feedback").dataset.outcome === '+JSON.stringify(outcome)+' && !document.getElementById("cleanup-feedback").textContent.includes("Čeká se")');
      }
      if(!await evaluate('document.getElementById("cleanup-panel").hidden && ["preserve","delete","stop"].every(mode=>document.getElementById("cleanup-"+mode).disabled)'))throw Error('Terminal cleanup controls remain available');
      await evaluate('document.getElementById("cleanup-preserve").click();true');
      console.log(JSON.stringify({terminal:outcome,latePost,feedback,consumedPolls:3}));
    }
    if(s2) {
      for(let i=0;i<100;i++) {
        const view=await call('Runtime.evaluate',{expression:'document.getElementById("cleanup-panel").hidden&&document.getElementById("cleanup-preserve").disabled',returnByValue:true},s2);
        if(view.result.value)break;
        if(i===99)throw Error('Second browser kept obsolete active decision');
        await new Promise(r=>setTimeout(r,100));
      }
      await call('Runtime.evaluate',{expression:'document.getElementById("cleanup-preserve").click()',returnByValue:true},s2);
    }
    const posts=requests.filter(r=>r.method==='POST'&&r.url===config.url+'/_service/control/cleanup');
    if(posts.length!==1||origins.get(posts[0].id)!==config.url)throw Error('Cleanup POST/Origin differs');
    if(latePost) {
      const response=responses.find(row=>row.requestId===posts[0].id);
      if(response?.status!==200)throw Error('Late POST acknowledgement was not received');
      const raw=await call('Network.getResponseBody',{requestId:response.requestId},s);
      if(JSON.parse(raw.base64Encoded?Buffer.from(raw.body,'base64').toString():raw.body).accepted!==true)throw Error('Late POST was not acknowledged');
    }
    const body=JSON.parse(posts[0].postData);
    const expected={source_sha256:config.source,approved_sha256:config.approved,proposal_sha256:config.proposal,
      actor:'Edge cleanup operator',csrf:detail.csrf,directories:config.mode==='cleanup-delete'?['Old']:[],confirm:true};
    if(config.mode==='cleanup-stop')expected.decision='stop';
    if(JSON.stringify(Object.keys(body).sort())!==JSON.stringify(Object.keys(expected).sort()) ||
       Object.keys(expected).some(key=>JSON.stringify(body[key])!==JSON.stringify(expected[key])))throw Error('Exact cleanup payload differs: '+JSON.stringify(body));
    const detailRequests=requests.filter(r=>r.url===config.url+'/_service/control/cleanup/proposal?sha='+config.proposal);
    if(detailRequests.length!==(s2?2:1))throw Error('Candidate detail was fetched again during polling');
    console.log(JSON.stringify({scenario:config.mode,appStopped:true,unixProxy:true,posts:1,browsers:s2?2:1,detailRequests:detailRequests.length}));
    return;
  }
  if(config.mode==='approval') {
    await call('Page.navigate',{url:config.url+config.path},s);
    await wait('!!document.getElementById("manifest-prepare-submit")');
    await wait('!!window.ReorganizationPresentation && !!document.querySelector("#manifest-warnings input")');
    for(const [width,height,mobile] of [[1366,768,false],[1600,900,false],[1920,1080,false],[2560,1440,false],[820,1180,true],[1180,820,true]]) {
      await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile},s);
      await call('Emulation.setTouchEmulationEnabled',{enabled:mobile,maxTouchPoints:5},s);
      if(await evaluate('document.documentElement.scrollWidth>innerWidth+1'))throw Error('Manifest horizontal overflow '+width);
      if(!await evaluate('document.getElementById("manifest-overview").innerText.includes("Přesunout:")&&!document.getElementById("manifest-technical").open'))throw Error('Manifest summary/technical disclosure');
      if(mobile) {
        const point=await evaluate('(()=>{const n=document.querySelector("#manifest-warnings label");n.scrollIntoView({block:"center"});const r=n.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()');
        for(const checked of [true,false]) {
          await call('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[point]},s);
          await call('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]},s);
          await wait('document.querySelector("#manifest-warnings input").checked==='+checked);
        }
      }
      await evaluate('document.getElementById("manifest-overview").scrollIntoView({block:"start"});true');
      const shot=await call('Page.captureScreenshot',{format:'png'},s);
      fs.writeFileSync(config.directory+'/manifest-'+width+'x'+height+'.png',Buffer.from(shot.data,'base64'));
    }
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
  await wait('document.getElementById("message").textContent.includes("čeká na výslovné rozhodnutí")');
  publish(3600);
  await wait('document.getElementById("result").textContent === "Stav neověřen"');
  if(await evaluate('document.getElementById("message").textContent.includes("Reorganizace probíhá")'))throw Error('Stale run shown live');
  data.active=false;data.outcome='STOP';data.phases[4].state='STOP';data.error={message:'Běh byl zastaven.'};publish();
  await wait('document.getElementById("result").dataset.outcome === "STOP"');
  if(!await evaluate('document.querySelector("[data-phase=sqlite]").innerText.includes("128 / 3095")'))throw Error('Lost confirmed count');
  data.outcome='NORMAL';data.phases.forEach(row=>row.state='PASS');publish(86400);
  await wait('document.getElementById("result").dataset.outcome === "NORMAL"');
  // A stale terminal record is explicitly a previous run, never a live execution.
  execFileSync('docker',['--context','default','start',config.backend],{stdio:'ignore'});
  await new Promise(resolve=>setTimeout(resolve,1200));
  if(await evaluate('document.body.innerText === "AnimeDB synthetic"'))throw Error('Health-only recovery bypassed host readiness');
  data.app_ready=true;data.source_sha256='b'.repeat(64);data.result_url='/reorganization/approval/'+'b'.repeat(64);publish();
  await wait('document.body.innerText === "AnimeDB synthetic"');
  console.log(JSON.stringify({ordinaryOutage:true,liveCounts:true,cleanupWait:true,staleUnverified:true,stop:true,historicalNormal:true,returnToApp:true,viewports:6}));
})().catch(error => {console.error(error.stack);process.exitCode=1;}).finally(() => {if(ws)ws.close();chrome.kill('SIGTERM');});
