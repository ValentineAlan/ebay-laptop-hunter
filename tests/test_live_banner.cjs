const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('app.py', 'utf8');
const start = source.lastIndexOf('(() => {{', source.indexOf('const POLL_MS = 20000'));
const end = source.indexOf('</script>', start);
const script = source.slice(start, end).replaceAll('{{', '{').replaceAll('}}', '}');
let payload = ['old', 'new', 'new'];
let rows = ['old'];
let poll;
let navigation;
const handlers = {};
const bar = {hidden: true, classList: {remove(){}, add(){}}};
const status = {textContent: ''};
const button = {hidden: true, addEventListener(type, fn){handlers[type] = fn;}};
const document = {hidden: false, getElementById(id){
    return {'live-update-bar':bar, 'live-update-status':status, 'live-update-button':button}[id]
        || {querySelectorAll(){return id === 'buy-now-deals-body' ? rows.map(itemId => ({dataset:{itemId}})) : [];}};
}, addEventListener(){}};
vm.runInNewContext(script, {document, URL, Date, Set, Array, fetch: async()=>({ok:true,json:async()=>({deal_ids:payload})}),
    window: {location:{href:'https://example.com/', replace(url){navigation=url;}},
        setInterval(fn){poll=fn;},setTimeout(){},addEventListener(type,fn){handlers[type]=fn;}}});
(async()=>{
    await poll();
    assert.equal(bar.hidden, false);
    assert.equal(status.textContent, '1 new deal available');
    handlers.click();
    assert.equal(bar.hidden, true);
    assert.ok(new URL(navigation).searchParams.has('show_new_deals'));
    rows = ['old','new'];
    handlers.pageshow();
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(bar.hidden, true);
    payload = ['old','new','later'];
    await poll();
    assert.equal(status.textContent, '1 new deal available');
    payload = ['old','new'];
    await poll();
    assert.equal(bar.hidden, true);
    console.log('Live banner interaction checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
