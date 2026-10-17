(() => {
  // Defaults stay on when a caller forgets __jevCfg. Python sets the object explicitly.
  function cfg() {
    const c = globalThis.__jevCfg || {};
    const frames = ["same", "all", "off"].includes(c.frames) ? c.frames : "same";
    return {
      shadow: c.shadow !== false && c.shadow !== 0 && c.shadow !== "0",
      frames,
      frameId: typeof c.frameId === "string" && c.frameId ? c.frameId : "f0",
    };
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
  function childDocument(iframe) {
    try { return iframe.contentDocument || null; }
    catch (error) { return null; }
  }

  function resolveHit(doc, x, y) {
    let hit = doc.elementFromPoint(x, y);
    const seen = new Set();
    while (cfg().shadow && hit && hit.shadowRoot && !seen.has(hit)) {
      seen.add(hit);
      const inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    if (cfg().frames !== "off" && hit && hit.tagName === "IFRAME") {
      const child = childDocument(hit);
      if (child) {
        const rect = hit.getBoundingClientRect();
        const inner = resolveHit(child, x - rect.x, y - rect.y);
        if (inner) return inner;
      }
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
      else if (node.ownerDocument && node.ownerDocument.defaultView) {
        let frame = null;
        try { frame = node.ownerDocument.defaultView.frameElement; } catch (error) { frame = null; }
        node = frame && frame !== node ? frame : null;
      } else node = null;
    }
    return false;
  }

  function possibleClosedShadow(element) {
    return !!(cfg().shadow && element.tagName && element.tagName.includes("-") && !element.shadowRoot);
  }

  // Same-origin frames are listed here. Cross-origin frames are recorded and left for Python.
  function frameEntries(doc, origin, top, alloc) {
    const entries = [];
    if (cfg().frames === "off" || !doc || typeof doc.querySelectorAll !== "function") return entries;
    let iframes = [];
    try { iframes = queryAllDeep(doc, "iframe"); } catch (error) { return entries; }
    for (const iframe of iframes) {
      const raw = iframe.getBoundingClientRect();
      const rect = {x: origin.x + raw.x, y: origin.y + raw.y, w: raw.width, h: raw.height};
      if (rect.w <= 0 || rect.h <= 0) continue;
      if (rect.x >= top.w || rect.x + rect.w <= 0 || rect.y >= top.h || rect.y + rect.h <= 0) continue;
      const child = childDocument(iframe);
      const same = !!(child && child.body);
      const id = alloc();
      entries.push({
        id, same, rect,
        src: iframe.getAttribute ? (iframe.getAttribute("src") || "") : "",
        url: iframe.src || "",
        doc: same ? child : null,
      });
      if (same) entries.push(...frameEntries(child, {x: rect.x, y: rect.y}, top, alloc));
    }
    return entries;
  }

  const api = {queryAllDeep, resolveHit, owns, possibleClosedShadow, frameEntries, childDocument, cfg};
  globalThis.__jevApi = api;
  if (typeof document === "undefined") return api;
  if (!document.body) return null;

  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1, frameWins:{}};
  cache.hit = (x, y) => resolveHit(document, x, y);
  cache.owns = owns;
  cache.frameWins[cfg().frameId] = window;
  function localCache(win) {
    const store = win.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1, frameWins:cache.frameWins};
    store.guard = cache.guard;
    return store;
  }
  const identity = e => {
    const win = (e.ownerDocument && e.ownerDocument.defaultView) || window;
    const store = localCache(win);
    if (!store.ids.has(e)) store.ids.set(e, store.next++);
    const id = store.ids.get(e);
    store.nodes.set(id, e);
    return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  cache.resolve = action => {
    const win = cache.frameWins[action.frame] || window;
    const store = win.__jevFast;
    return store ? store.nodes.get(action.node) : null;
  };
  cache.toTop = e => {
    const rect = e.getBoundingClientRect();
    let x = rect.x + rect.width / 2, y = rect.y + rect.height / 2, win = e.ownerDocument.defaultView;
    const seen = new Set();
    while (win && win !== win.top && !seen.has(win)) {
      seen.add(win);
      let frame = null;
      try { frame = win.frameElement; } catch (error) { frame = null; }
      if (!frame) break;
      const host = frame.getBoundingClientRect();
      x += host.x; y += host.y;
      win = win.parent;
    }
    return {x, y};
  };
  const safe = e => !['password','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const doc = e.ownerDocument || document;
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(doc.getElementById(id),seen)).filter(Boolean).join(' ');
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
      if (e.type==='file') return 'button';
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  function documentKey(doc) {
    const win = doc.defaultView || window;
    return [win.performance.timeOrigin, doc.location.href, win.scrollX, win.scrollY, win.innerWidth, win.innerHeight,
      queryAllDeep(doc,'input,textarea,select').filter(safe)
        .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  }
  cache.pageKey=()=>{
    const keys=[documentKey(document)];
    if (cfg().frames==='off') return keys;
    let serial=0;
    const root=cfg().frameId;
    const alloc=()=>{ serial+=1; return root==='f0' ? 'f'+serial : root+'.'+serial; };
    for (const entry of frameEntries(document, {x:0,y:0}, {w:innerWidth,h:innerHeight}, alloc)) {
      if (entry.same && entry.doc) {
        try { keys.push(documentKey(entry.doc)); } catch (error) {}
      }
    }
    return keys;
  };
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  const actions=[];
  const seenNodes=new Set();
  function consider(e, frameId, ox, oy) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) return;
    const raw=e.getBoundingClientRect();
    const rect={x:raw.x+ox, y:raw.y+oy, w:raw.width, h:raw.height};
    const x=rect.x+rect.w/2, y=rect.y+rect.h/2, rname=role(e);
    if (!rname || rect.w<=0 || rect.h<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) return;
    const node=identity(e);
    const base={node, frame:frameId, role:rname, label:name(e)||rname, rect};
    if (possibleClosedShadow(e)) base.possible_closed_shadow=true;
    seenNodes.add(frameId+':'+node);
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='INPUT' && e.type==='file') {
      actions.push({...base, kind:'upload', value:''});
      return;
    }
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(item=>item.label).join(', '),label:base.label+' → '+o.label});
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
  function collectClosed(doc, frameId, ox, oy) {
    if (!cfg().shadow) return;
    for (const e of queryAllDeep(doc, '*')) {
      if (!possibleClosedShadow(e) || !visible(e) || e.matches(':disabled') || e.querySelector(selector)) continue;
      const label=(name(e)||'').trim();
      if (!label || label.length>80) continue;
      const raw=e.getBoundingClientRect();
      const rect={x:raw.x+ox, y:raw.y+oy, w:raw.width, h:raw.height};
      const x=rect.x+rect.w/2, y=rect.y+rect.h/2;
      if (rect.w<=0 || rect.h<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
      const node=identity(e);
      const key=frameId+':'+node;
      if (seenNodes.has(key)) continue;
      seenNodes.add(key);
      actions.push({node, frame:frameId, role:'button', label, kind:'click', value:'',
        possible_closed_shadow:true, rect});
    }
  }
  let frameSerial=0;
  const rootFrame=cfg().frameId;
  const allocFrame=()=>{ frameSerial+=1; return rootFrame==='f0' ? 'f'+frameSerial : rootFrame+'.'+frameSerial; };
  const frames=cfg().frames==='off' ? [] : frameEntries(document,{x:0,y:0},{w:innerWidth,h:innerHeight},allocFrame);
  for (const entry of frames) if (entry.same && entry.doc && entry.doc.defaultView) {
    cache.frameWins[entry.id]=entry.doc.defaultView;
    localCache(entry.doc.defaultView);
  }
  for (const e of queryAllDeep(document, selector)) consider(e, rootFrame, 0, 0);
  collectClosed(document, rootFrame, 0, 0);
  for (const entry of frames) if (entry.same && entry.doc) {
    for (const e of queryAllDeep(entry.doc, selector)) consider(e, entry.id, entry.rect.x, entry.rect.y);
    collectClosed(entry.doc, entry.id, entry.rect.x, entry.rect.y);
  }
  const words=[]; let length=0;
  function addText(scope, ox, oy) {
    if (!scope || length>=6000) return;
    const owner=scope.nodeType===9 ? scope : scope.ownerDocument;
    const start=scope.nodeType===9 ? scope.body : scope;
    if (!owner || !start || !owner.createTreeWalker) return;
    const walker=owner.createTreeWalker(start,NodeFilter.SHOW_TEXT);
    const range=owner.createRange();
    let node;
    while ((node=walker.nextNode()) && length<6000) {
      const value=node.textContent.trim(), parent=node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node); const raw=range.getBoundingClientRect();
      const top=raw.top+oy, bottom=raw.bottom+oy, left=raw.left+ox, right=raw.right+ox;
      if (raw.width>0 && raw.height>0 && bottom>0 && top<innerHeight && right>0 && left<innerWidth) {
        words.push(value); length+=value.length;
      }
    }
  }
  addText(document, 0, 0);
  if (cfg().shadow) {
    for (const host of queryAllDeep(document, '*')) if (host.shadowRoot) addText(host.shadowRoot, 0, 0);
  }
  for (const entry of frames) if (entry.same && entry.doc) {
    addText(entry.doc, entry.rect.x, entry.rect.y);
    if (cfg().shadow) {
      for (const host of queryAllDeep(entry.doc, '*')) if (host.shadowRoot) addText(host.shadowRoot, entry.rect.x, entry.rect.y);
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) {
    const key=(a.frame||rootFrame)+':'+a.node;
    if (key in guards) continue;
    const win=cache.frameWins[a.frame]||window;
    const store=win.__jevFast;
    guards[key]=store ? store.guard(store.nodes.get(a.node)) : null;
  }
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>{
    const prefix=a.frame && a.frame!=='f0' ? a.frame+':' : '';
    a.id=prefix+'e'+(i+1);
  });
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  const cross_frames=frames.filter(entry=>!entry.same).map(({id,rect,src,url})=>({id,rect,src,url}));
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions,cross_frames};
})()
