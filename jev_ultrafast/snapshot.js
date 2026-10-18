(() => {
  // Defaults stay on when a caller forgets __jevCfg. Python sets the object explicitly.
  function cfg() {
    const c = globalThis.__jevCfg || {};
    const frames = ["same", "all", "off"].includes(c.frames) ? c.frames : "same";
    return {
      shadow: c.shadow !== false && c.shadow !== 0 && c.shadow !== "0",
      frames,
      frameId: typeof c.frameId === "string" && c.frameId ? c.frameId : "f0",
      nested: c.nested !== false && c.nested !== 0 && c.nested !== "0",
      largeSelect: c.largeSelect !== false && c.largeSelect !== 0 && c.largeSelect !== "0",
      selectInlineMax: Number.isFinite(+c.selectInlineMax) ? +c.selectInlineMax : 12,
      vision: c.vision === true || c.vision === 1 || c.vision === "1",
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

  function scrollTargets(candidates) {
    const ranked = candidates
      .filter(item => item && (item.vertical || item.horizontal))
      .sort((a, b) => b.area - a.area)
      .slice(0, 8);
    const actions = [];
    for (const item of ranked) {
      const label = item.label || "container";
      if (item.vertical) {
        const delta = Math.max(1, Math.round(item.clientHeight * 0.8));
        actions.push({kind:"scroll", node:item.node, frame:item.frame, axis:"y", delta,
          label:"Scroll down " + label});
        actions.push({kind:"scroll", node:item.node, frame:item.frame, axis:"y", delta:-delta,
          label:"Scroll up " + label});
      }
      if (item.horizontal) {
        const delta = Math.max(1, Math.round(item.clientWidth * 0.8));
        actions.push({kind:"scroll", node:item.node, frame:item.frame, axis:"x", delta,
          label:"Scroll right " + label});
        actions.push({kind:"scroll", node:item.node, frame:item.frame, axis:"x", delta:-delta,
          label:"Scroll left " + label});
      }
    }
    return actions;
  }

  const api = {queryAllDeep, resolveHit, owns, possibleClosedShadow, frameEntries, scrollTargets, childDocument, cfg};
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
  const CHECKABLE = e => (e.tagName==='INPUT' && ['checkbox','radio'].includes(e.type)) ||
    ['checkbox','radio','switch'].includes(e.getAttribute('role'));
  // An aria-hidden element that still paints on top (a popup its site forgot to
  // unhide) is usable: hit-testing, not the attribute, decides for actions.
  // Native checkables are transparent by design; their opacity never disqualifies them.
  const shown = (e, keepOpacity=true) => {
    if (e.closest('[inert]')) return false;
    if (!e.checkVisibility({checkVisibilityCSS:true})) return false;
    if (!e.closest('[aria-hidden="true"]')) return !keepOpacity || e.checkVisibility({checkOpacity:true});
    const raw=e.getBoundingClientRect();
    return owns(e, resolveHit(e.ownerDocument||document, raw.x+raw.width/2, raw.y+raw.height/2));
  };
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
    if (!safe(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) return;
    if (!shown(e, !CHECKABLE(e))) return;
    const raw=e.getBoundingClientRect();
    const rect={x:raw.x+ox, y:raw.y+oy, w:raw.width, h:raw.height};
    const x=rect.x+rect.w/2, y=rect.y+rect.h/2, rname=role(e);
    if (!rname || rect.w<=0 || rect.h<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) return;
    const node=identity(e);
    const base={node, frame:frameId, role:rname, label:name(e)||rname, rect};
    if (possibleClosedShadow(e)) base.possible_closed_shadow=true;
    seenNodes.add(frameId+':'+node);
    for (const key of ['checked','selected','expanded','pressed']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (['checkbox','radio','switch','option'].includes(rname)) {
      const group=e.closest('fieldset,[role="radiogroup"],[role="group"],[role="listbox"],[role="menu"]');
      const legend=group && group.tagName==='FIELDSET' ? group.querySelector('legend') : null;
      const glabel=((legend && legend.innerText) || (group && group.getAttribute('aria-label')) || '').trim();
      if (glabel) base.label=`${base.label} (${glabel.slice(0,40)})`;
    }
    if (e.closest('[role="grid"],[role="treegrid"]')) base.in_grid=true;
    if (e.closest('nav,header,footer,[role="navigation"],[role="banner"],[role="contentinfo"]')) base.nav=true;
    if (e.tagName==='INPUT' && e.type==='file') {
      actions.push({...base, kind:'upload', value:''});
      return;
    }
    if (e.tagName==='SELECT') {
      const enabled=[...e.options].filter(o=>!o.disabled && !o.closest('optgroup[disabled]'));
      const current=[...e.selectedOptions].map(item=>item.label).join(', ');
      const useOne=cfg().largeSelect && enabled.length>cfg().selectInlineMax;
      if (useOne) {
        actions.push({...base, kind:'select_option', value:'', current_value:current, option_count:enabled.length});
      } else {
        for (const o of enabled) if (!o.selected)
          actions.push({...base,kind:'select',value:o.value,current_value:current,label:base.label+' → '+o.label});
      }
    } else if (['checkbox','radio','switch'].includes(rname)) {
      actions.push({...base, kind:'check', desired:true, value:'', label:'Set '+base.label});
      if (base.checked==='true')
        actions.push({...base, kind:'check', desired:false, value:'', label:'Clear '+base.label});
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
      if (!possibleClosedShadow(e) || !shown(e) || e.matches(':disabled') || e.querySelector(selector)) continue;
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
  // A genuinely open overlay sorts first; a true modal scopes choices to itself.
  // Background stays available for non-modal popups (the field behind autocomplete).
  let modal=null;
  for (const d of queryAllDeep(document, 'dialog,[role="dialog"],[role="alertdialog"]')) {
    if (d.closest('[inert]')) continue;
    const r=d.getBoundingClientRect();
    if (r.width<=0 || r.height<=0) continue;
    const x=r.x+r.width/2, y=r.y+r.height/2;
    if (x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (d.closest('[aria-hidden="true"]') && !owns(d, resolveHit(document,x,y))) continue;
    modal=d;
  }
  if (modal) {
    const scoped=modal.tagName==='DIALOG' ? !!modal.open : modal.getAttribute('aria-modal')==='true';
    const sameDoc=action=>!action.frame || action.frame===rootFrame;
    const inside=action=>{
      const n=cache.nodes.get(action.node);
      return n && (n===modal || modal.contains(n));
    };
    const first=[], rest=[];
    for (const action of actions) ((action.kind==='scroll'||action.kind==='wait'||!sameDoc(action)||inside(action)) ? first : rest).push(action);
    actions.length=0;
    actions.push(...first);
    if (!scoped) actions.push(...rest);
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
      if (!value || !parent || parent.closest('script,style,noscript,template') || !shown(parent)) continue;
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
  if (cfg().nested) {
    const candidates=[];
    function addContainers(doc, frameId, ox, oy) {
      if (!doc || typeof doc.querySelectorAll!=='function') return;
      let nodes=[];
      try { nodes=queryAllDeep(doc, '*'); } catch (error) { return; }
      for (const e of nodes) {
        if (e===document.body || e===document.documentElement || !visible(e)) continue;
        const style=getComputedStyle(e);
        const vertical=['auto','scroll','overlay'].includes(style.overflowY) && e.scrollHeight>e.clientHeight+4;
        const horizontal=['auto','scroll','overlay'].includes(style.overflowX) && e.scrollWidth>e.clientWidth+4;
        if (!vertical && !horizontal) continue;
        const raw=e.getBoundingClientRect();
        const rect={x:raw.x+ox, y:raw.y+oy, w:raw.width, h:raw.height};
        if (rect.w<8 || rect.h<8 || rect.x>=innerWidth || rect.x+rect.w<=0 || rect.y>=innerHeight || rect.y+rect.h<=0) continue;
        const area=Math.max(0, Math.min(rect.x+rect.w, innerWidth)-Math.max(rect.x, 0))
          * Math.max(0, Math.min(rect.y+rect.h, innerHeight)-Math.max(rect.y, 0));
        candidates.push({
          node:identity(e), frame:frameId, vertical, horizontal, area,
          clientHeight:e.clientHeight, clientWidth:e.clientWidth,
          label:(name(e)||'container').trim().slice(0, 40),
        });
      }
    }
    addContainers(document, rootFrame, 0, 0);
    for (const entry of frames) if (entry.same && entry.doc) addContainers(entry.doc, entry.id, entry.rect.x, entry.rect.y);
    let n=0;
    for (const action of scrollTargets(candidates)) {
      n+=1;
      action.id='scroll:'+(action.frame||rootFrame)+':'+action.node+':'+n;
      actions.push(action);
    }
  }
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  const cross_frames=frames.filter(entry=>!entry.same).map(({id,rect,src,url})=>({id,rect,src,url}));
  const surfaces=[];
  if (cfg().vision) {
    for (const e of queryAllDeep(document, 'canvas,svg')) {
      if (!visible(e)) continue;
      const raw=e.getBoundingClientRect();
      if (raw.width<8 || raw.height<8 || raw.right<=0 || raw.bottom<=0 || raw.left>=innerWidth || raw.top>=innerHeight) continue;
      surfaces.push({id:'s'+surfaces.length, tag:e.tagName.toLowerCase(),
        rect:{x:raw.x, y:raw.y, w:raw.width, h:raw.height}});
    }
  }
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions,cross_frames,surfaces};
})()
