(() => {
  // Defaults stay on when a caller forgets __jevCfg. Python sets the object explicitly.
  function cfg() {
    const c = globalThis.__jevCfg || {};
    return {shadow: c.shadow !== false && c.shadow !== 0 && c.shadow !== "0"};
  }

  // Light-DOM order matches querySelectorAll. Open shadow roots are appended, depth-first.
  function queryAllDeep(root, selector) {
    const shadow = cfg().shadow;
    const out = [];
    const seen = new Set();
    function visit(scope) {
      if (!scope || typeof scope.querySelectorAll !== "function" || seen.has(scope)) return;
      seen.add(scope);
      for (const e of scope.querySelectorAll(selector)) out.push(e);
      if (!shadow) return;
      for (const host of scope.querySelectorAll("*")) if (host.shadowRoot) visit(host.shadowRoot);
    }
    visit(root);
    return out;
  }

  // elementFromPoint stops at a shadow host. Step into each open root.
  function resolveHit(doc, x, y) {
    let hit = doc.elementFromPoint(x, y);
    const seen = new Set();
    while (cfg().shadow && hit && hit.shadowRoot && !seen.has(hit)) {
      seen.add(hit);
      const inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    return hit;
  }

  // Accept the observed node or a descendant, crossing open shadow boundaries.
  function owns(target, hit) {
    if (!target || !hit) return false;
    let node = hit;
    const seen = new Set();
    while (node && !seen.has(node)) {
      if (node === target) return true;
      seen.add(node);
      if (node.parentElement) node = node.parentElement;
      else if (node.parentNode && node.parentNode.host) node = node.parentNode.host;
      else node = null;
    }
    return false;
  }

  function possibleClosedShadow(element) {
    return !!(cfg().shadow && element.tagName && element.tagName.includes("-") && !element.shadowRoot);
  }

  const api = {queryAllDeep, resolveHit, owns, possibleClosedShadow, cfg};
  globalThis.__jevApi = api;
  if (typeof document === "undefined") return api;
  if (!document.body) return null;

  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  cache.hit = (x, y) => resolveHit(document, x, y);
  cache.owns = owns;
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    queryAllDeep(document,'input,textarea,select').filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  const actions=[];
  for (const e of queryAllDeep(document, selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    if (possibleClosedShadow(e)) base.possible_closed_shadow = true;
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  // A closed shadow host is not one of the usual controls. Offer the host itself so a
  // no-progress click can be explained. Skip hosts that already contain a light-DOM control.
  if (cfg().shadow) {
    const seenNodes = new Set(actions.map(a => a.node));
    for (const e of queryAllDeep(document, '*')) {
      if (!possibleClosedShadow(e) || !visible(e) || e.matches(':disabled')) continue;
      if (e.querySelector(selector)) continue;
      const label = (name(e) || '').trim();
      if (!label || label.length > 80) continue;
      const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
      if (r.width <= 0 || r.height <= 0 || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      const node = identity(e);
      if (seenNodes.has(node)) continue;
      seenNodes.add(node);
      actions.push({node, role:'button', label, kind:'click', value:'', possible_closed_shadow:true,
        rect:{x:r.x, y:r.y, w:r.width, h:r.height}});
    }
  }
  const words=[]; const range=document.createRange(); let length=0;
  function addText(scope) {
    if (!scope || length>=6000) return;
    const start = scope.nodeType===9 ? scope.body : scope;
    if (!start) return;
    const walker=document.createTreeWalker(start,NodeFilter.SHOW_TEXT);
    let node;
    while ((node=walker.nextNode()) && length<6000) {
      const value=node.textContent.trim(), parent=node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node); const r=range.getBoundingClientRect();
      if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
        words.push(value); length+=value.length;
      }
    }
  }
  addText(document);
  if (cfg().shadow) {
    for (const host of queryAllDeep(document, '*')) if (host.shadowRoot) addText(host.shadowRoot);
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions};
})()
