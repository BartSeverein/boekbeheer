(function () {
  'use strict';
  // De teksten per taal worden bij het bouwen ingevuld (webshop/i18n.py, sleutels die met js_ beginnen).
  var I18N = /*I18N*/{};
  var LANG = document.documentElement.lang || 'nl', T = I18N[LANG] || I18N.nl || {};
  var KEY = 'cart1', body = document.body;
  var D = body.dataset, EXTRA = +D.extra || 0, FREE = +D.free || 0, DEF = +D.def || 395;
  var P = {}, RT = {};
  try { P = JSON.parse(D.p || '{}'); RT = JSON.parse(D.rt || '{}'); } catch (e) {}

  var ICON_DEL = '<svg class="i" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false"><path d="M9 3v1H4v2h1v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6h1V4h-5V3zM7 6h10v13H7zm2 2v9h2V8zm4 0v9h2V8z"/></svg>';
  function fmt(s, o) { return String(s || '').replace(/\{(\w+)\}/g, function (m, k) { return o && o[k] != null ? o[k] : m; }); }
  function load() {
    try { var c = JSON.parse(localStorage.getItem(KEY) || '[]'); return Array.isArray(c) ? c : []; }
    catch (e) { return []; }
  }
  function save(c) { try { localStorage.setItem(KEY, JSON.stringify(c)); } catch (e) {} badge(); }
  function count(c) { return c.reduce(function (a, b) { return a + b.q; }, 0); }
  function badge() {
    var el = document.getElementById('cartlabel'), n = count(load());
    if (el) el.textContent = T.cart + (n ? ' (' + n + ')' : '');
  }
  // Zelfde schrijfwijze als eur() in common.py: nl € 9,45 · en € 9.45 · de/fr 9,45 €
  function eur(c) {
    var neg = c < 0 ? '-' : '', a = Math.abs(c), e = Math.floor(a / 100), r = ('0' + (a % 100)).slice(-2);
    var big = e >= 10000;
    if (LANG === 'en') return neg + '€ ' + (big ? String(e).replace(/\B(?=(\d{3})+(?!\d))/g, ',') : e) + '.' + r;
    if (LANG === 'de' || LANG === 'fr')
      return neg + (big ? String(e).replace(/\B(?=(\d{3})+(?!\d))/g, LANG === 'fr' ? ' ' : '.') : e) + ',' + r + ' €';
    return neg + '€ ' + (big ? String(e).replace(/\B(?=(\d{3})+(?!\d))/g, '.') : e) + ',' + r;
  }
  // Zelfde regel als in lib.php (shipping_for): tarief van het land (klein pakje, of groot als één boek groot is)
  // + toeslag per extra boek; OTHER geldt voor landen zonder eigen tarief. Gratis verzenden geldt alleen voor Nederland.
  function totals(c, country) {
    country = country || 'NL';
    var sub = 0, big = 0, n = 0;
    c.forEach(function (i) { sub += i.p * i.q; n += i.q; if (i.g) big = 1; });
    if (!n) return { sub: 0, ship: 0, total: 0 };
    var rate = RT[country] || RT.OTHER || RT.NL || [DEF, DEF];
    var ship = rate[big] + EXTRA * (n - 1);
    if (FREE && sub >= FREE && country === 'NL') ship = 0;
    return { sub: sub, ship: ship, total: sub + ship };
  }
  function h(tag, attrs) {
    var el = document.createElement(tag);
    for (var k in (attrs || {})) { if (k === 'text') el.textContent = attrs[k]; else el.setAttribute(k, attrs[k]); }
    for (var i = 2; i < arguments.length; i++) if (arguments[i]) el.appendChild(arguments[i]);
    return el;
  }
  function $(id) { return document.getElementById(id); }

  // --- Fotogalerij: een klein plaatje wisselt van plek met de grote foto ---
  var mainImg = document.querySelector('.book img.mi');
  if (mainImg) {
    document.querySelectorAll('.book .thumbs a').forEach(function (a) {
      a.addEventListener('click', function (ev) {
        var t = a.querySelector('img');
        if (!t) return;
        ev.preventDefault();
        var m = { src: mainImg.getAttribute('src'), w: +mainImg.dataset.w, h: +mainImg.dataset.h };
        var n = { src: t.getAttribute('src'), w: +t.dataset.w, h: +t.dataset.h };
        mainImg.setAttribute('src', n.src); mainImg.dataset.w = n.w; mainImg.dataset.h = n.h;
        mainImg.setAttribute('width', n.w); mainImg.setAttribute('height', n.h);
        var tw = 120, th = Math.max(1, Math.round(tw * m.h / m.w));
        t.setAttribute('src', m.src); t.dataset.w = m.w; t.dataset.h = m.h;
        t.setAttribute('width', tw); t.setAttribute('height', th);
        a.setAttribute('href', m.src);
      });
    });
  }

  // --- Toevoegen aan winkelwagen (boekpagina's) ---
  var add = document.querySelector('button.add');
  if (add) {
    var d = add.dataset;
    var refresh = function () {
      var it = load().filter(function (x) { return x.id === +d.id; })[0];
      var full = it && it.q >= +d.max;
      add.disabled = !!full;
      var lab = add.querySelector('span'); (lab || add).textContent = full ? T.in_cart : T.add;
    };
    add.addEventListener('click', function () {
      var c = load(), it = c.filter(function (x) { return x.id === +d.id; })[0];
      if (it) { if (it.q < +d.max) it.q++; }
      else c.push({ id: +d.id, t: d.title, p: +d.price, s: +d.ship, g: +d.big || 0, u: d.url, m: +d.max, q: 1 });
      save(c); refresh();
      var m = $('added'); if (m) m.hidden = false;
    });
    refresh();
  }

  // --- Winkelwagenpagina ---
  var cartEl = $('cart');
  if (cartEl) {
    var bookHref = function (u) {
      var m = /^\/boek\/([a-z0-9-]+)\/$/.exec(u || '');
      return m ? (P.book || '/boek/') + m[1] + '/' : (P.home || '/');
    };
    var drawCart = function () {
      var c = load(); cartEl.textContent = '';
      if (!c.length) { cartEl.appendChild(h('p', { text: T.empty })); return; }
      var tb = h('tbody');
      c.forEach(function (i) {
        var rm = h('button', { type: 'button', class: 'ghost', title: T.remove, 'aria-label': T.remove });
        rm.innerHTML = ICON_DEL;  // vaste, eigen SVG (mdi trash-can-outline); geen gegevens van buiten
        rm.addEventListener('click', function () { save(load().filter(function (x) { return x.id !== i.id; })); drawCart(); });
        tb.appendChild(h('tr', {}, h('td', {}, h('a', { href: bookHref(i.u), text: i.t })),
          h('td', { class: 'n', text: eur(i.p) + (i.q > 1 ? ' × ' + i.q : '') }), h('td', {}, rm)));
      });
      var t = totals(c);
      cartEl.appendChild(h('table', {}, tb));
      cartEl.appendChild(h('p', { text: T.books + ': ' + eur(t.sub) + ' · ' + T.shipping + ': ' + (t.ship ? eur(t.ship) : T.free) }));
      if (Object.keys(RT).length > 1) cartEl.appendChild(h('p', { class: 'small', text: T.abroad }));
      cartEl.appendChild(h('p', { class: 'price', text: T.total + ': ' + eur(t.total) }));
      cartEl.appendChild(h('a', { class: 'btn', href: P.checkout || '/afrekenen/', text: T.checkout }));
    };
    drawCart();
  }

  // --- Afrekenen ---
  var form = $('checkout');
  if (form) {
    var sum = $('sum'), msg = $('msg'), btn = form.querySelector('button[type=submit]'), sel = form.querySelector('select[name=country]');
    var country = function () { return sel ? sel.value : 'NL'; };
    var paint = function () {
      var c = load(), t = totals(c, country());
      sum.textContent = c.length ? fmt(T.sum, { total: eur(t.total), sub: eur(t.sub), ship: t.ship ? eur(t.ship) : T.free }) : T.empty;
      btn.disabled = !c.length;
    };
    paint();
    if (sel) sel.addEventListener('change', paint);
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var c = load(); if (!c.length) return;
      var f = new FormData(form), cust = {};
      ['name', 'email', 'street', 'postcode', 'city', 'country', 'note'].forEach(function (k) { cust[k] = (f.get(k) || '').toString().trim(); });
      btn.disabled = true; msg.hidden = false; msg.textContent = T.wait;
      fetch('/checkout.php', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: c.map(function (i) { return { id: i.id, q: i.q }; }), customer: cust, lang: LANG,
          expected: totals(c, country()).total, hp: (f.get('website') || '').toString() })
      }).then(function (r) { return r.json().catch(function () { return { ok: false, error: T.unexpected }; }); })
        .then(function (r) {
          if (r.ok && r.url) { window.location.href = r.url; return; }
          if (r.unavailable && r.unavailable.length) {
            save(load().filter(function (x) { return r.unavailable.indexOf(x.id) < 0; })); paint();
          }
          if (r.prices) {
            var cc = load(); cc.forEach(function (i) { if (r.prices[i.id]) { i.p = r.prices[i.id].p; i.s = r.prices[i.id].s; i.g = r.prices[i.id].g; } }); save(cc); paint();
          }
          msg.textContent = r.error || T.oops; btn.disabled = !load().length;
        }).catch(function () { msg.textContent = T.noconn; btn.disabled = false; });
    });
  }

  // --- Bedankpagina: status ophalen, winkelwagen pas leegmaken als betaald ---
  var th = $('thanks');
  if (th) {
    var tok = (location.search.match(/[?&]o=([a-f0-9]{32})/) || [])[1], tries = 0;
    var poll = function () {
      if (!tok) { th.textContent = T.t_none; return; }
      fetch('/status.php?o=' + tok).then(function (r) { return r.json(); }).then(function (r) {
        if (r.status === 'paid') { save([]); th.textContent = T.t_paid; }
        else if (r.status === 'open' && tries++ < 12) { th.textContent = T.t_wait; setTimeout(poll, 2500); }
        else if (r.status === 'open') { th.textContent = T.t_open; }
        else { th.textContent = T.t_failed; }
      }).catch(function () { th.textContent = T.t_err; });
    };
    poll();
  }

  // --- Zoeken ---
  var res = $('results');
  if (res) {
    var q = (new URLSearchParams(location.search).get('q') || '').trim(), page = 1;
    var qi = document.querySelector('header input[name=q]'); if (qi) qi.value = q;
    var more = $('more'), info = $('info');
    var card = function (b) {
      var img = b.img ? h('img', { src: b.img, width: b.w, height: b.h, alt: '', loading: 'lazy' }) : null;
      return h('li', { class: 'card' }, h('a', { href: b.url }, h('div', { class: 'ph' }, img),
        h('h3', { text: b.title }), h('p', { text: b.author || '' }), h('p', { class: 'pr', text: eur(b.price) })));
    };
    var run = function () {
      fetch('/search.php?q=' + encodeURIComponent(q) + '&page=' + page + '&lang=' + encodeURIComponent(LANG)).then(function (r) { return r.json(); }).then(function (r) {
        if (!r.ok) { info.textContent = r.error || T.s_fail; return; }
        r.items.forEach(function (b) { res.appendChild(card(b)); });
        info.textContent = r.total ? r.total + ' ' + (r.total === 1 ? T.s_one : T.s_many) : T.s_none;
        more.hidden = page * r.per_page >= r.total;
      }).catch(function () { info.textContent = T.s_fail; });
    };
    if (q.length >= 2) { info.textContent = T.s_wait; run(); more.addEventListener('click', function () { page++; run(); }); }
    else info.textContent = T.s_min;
  }

  // Taalkeuze: zoekterm en bestelcode blijven behouden als je van taal wisselt.
  if (location.search) {
    var links = document.querySelectorAll('nav.lang a');
    for (var li = 0; li < links.length; li++) links[li].href += location.search;
  }
  badge();
})();
