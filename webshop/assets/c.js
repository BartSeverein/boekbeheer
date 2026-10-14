(function () {
  'use strict';
  var KEY = 'cart1', body = document.body;
  var D = body.dataset, EXTRA = +D.extra || 0, FREE = +D.free || 0, DEF = +D.def || 395;

  function load() {
    try { var c = JSON.parse(localStorage.getItem(KEY) || '[]'); return Array.isArray(c) ? c : []; }
    catch (e) { return []; }
  }
  function save(c) { try { localStorage.setItem(KEY, JSON.stringify(c)); } catch (e) {} badge(); }
  function count(c) { return c.reduce(function (a, b) { return a + b.q; }, 0); }
  function badge() {
    var el = document.getElementById('cartlink'), n = count(load());
    if (el) el.textContent = 'Winkelwagen' + (n ? ' (' + n + ')' : '');
  }
  function eur(c) {
    var e = Math.floor(Math.abs(c) / 100), r = ('0' + (Math.abs(c) % 100)).slice(-2);
    var t = e >= 10000 ? String(e).replace(/\B(?=(\d{3})+(?!\d))/g, '.') : String(e);
    return (c < 0 ? '-' : '') + '€ ' + t + ',' + r;
  }
  // Zelfde regel als in lib.php (shipping_for): hoogste verzendprijs + toeslag per extra boek, eventueel gratis.
  function totals(c) {
    var sub = 0, ship = 0, n = 0;
    c.forEach(function (i) { sub += i.p * i.q; n += i.q; if (i.s > ship) ship = i.s; });
    if (!n) return { sub: 0, ship: 0, total: 0 };
    ship += EXTRA * (n - 1);
    if (FREE && sub >= FREE) ship = 0;
    return { sub: sub, ship: ship, total: sub + ship };
  }
  function h(tag, attrs) {
    var el = document.createElement(tag);
    for (var k in (attrs || {})) { if (k === 'text') el.textContent = attrs[k]; else el.setAttribute(k, attrs[k]); }
    for (var i = 2; i < arguments.length; i++) if (arguments[i]) el.appendChild(arguments[i]);
    return el;
  }
  function $(id) { return document.getElementById(id); }

  // --- Toevoegen aan winkelwagen (boekpagina's) ---
  var add = document.querySelector('button.add');
  if (add) {
    var d = add.dataset;
    var refresh = function () {
      var it = load().filter(function (x) { return x.id === +d.id; })[0];
      var full = it && it.q >= +d.max;
      add.disabled = !!full;
      add.textContent = full ? 'In winkelwagen' : 'In winkelwagen leggen';
    };
    add.addEventListener('click', function () {
      var c = load(), it = c.filter(function (x) { return x.id === +d.id; })[0];
      if (it) { if (it.q < +d.max) it.q++; }
      else c.push({ id: +d.id, t: d.title, p: +d.price, s: +d.ship, u: d.url, m: +d.max, q: 1 });
      save(c); refresh();
      var m = $('added'); if (m) m.hidden = false;
    });
    refresh();
  }

  // --- Winkelwagenpagina ---
  var cartEl = $('cart');
  if (cartEl) {
    var drawCart = function () {
      var c = load(); cartEl.textContent = '';
      if (!c.length) { cartEl.appendChild(h('p', { text: 'Je winkelwagen is leeg.' })); return; }
      var tb = h('tbody');
      c.forEach(function (i) {
        var rm = h('button', { type: 'button', text: 'Verwijder' });
        rm.addEventListener('click', function () { save(load().filter(function (x) { return x.id !== i.id; })); drawCart(); });
        tb.appendChild(h('tr', {}, h('td', {}, h('a', { href: /^\/boek\/[a-z0-9-]+\/$/.test(i.u || '') ? i.u : '/', text: i.t })),
          h('td', { class: 'n', text: eur(i.p) + (i.q > 1 ? ' × ' + i.q : '') }), h('td', {}, rm)));
      });
      var t = totals(c);
      cartEl.appendChild(h('table', {}, tb));
      cartEl.appendChild(h('p', { text: 'Boeken: ' + eur(t.sub) + ' · Verzendkosten: ' + (t.ship ? eur(t.ship) : 'gratis') }));
      cartEl.appendChild(h('p', { class: 'price', text: 'Totaal: ' + eur(t.total) }));
      cartEl.appendChild(h('a', { class: 'btn', href: '/afrekenen/', text: 'Afrekenen' }));
    };
    drawCart();
  }

  // --- Afrekenen ---
  var form = $('checkout');
  if (form) {
    var sum = $('sum'), msg = $('msg'), btn = form.querySelector('button[type=submit]');
    var paint = function () {
      var c = load(), t = totals(c);
      sum.textContent = c.length ? 'Totaal: ' + eur(t.total) + ' (boeken ' + eur(t.sub) + ' + verzending ' + (t.ship ? eur(t.ship) : 'gratis') + ')' : 'Je winkelwagen is leeg.';
      btn.disabled = !c.length;
    };
    paint();
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var c = load(); if (!c.length) return;
      var f = new FormData(form), cust = {};
      ['name', 'email', 'street', 'postcode', 'city', 'country', 'note'].forEach(function (k) { cust[k] = (f.get(k) || '').toString().trim(); });
      btn.disabled = true; msg.hidden = false; msg.textContent = 'Even geduld...';
      fetch('/checkout.php', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: c.map(function (i) { return { id: i.id, q: i.q }; }), customer: cust, expected: totals(c).total, hp: (f.get('website') || '').toString() })
      }).then(function (r) { return r.json().catch(function () { return { ok: false, error: 'Onverwacht antwoord van de server.' }; }); })
        .then(function (r) {
          if (r.ok && r.url) { window.location.href = r.url; return; }
          if (r.unavailable && r.unavailable.length) {
            save(load().filter(function (x) { return r.unavailable.indexOf(x.id) < 0; })); paint();
          }
          if (r.prices) {
            var cc = load(); cc.forEach(function (i) { if (r.prices[i.id]) { i.p = r.prices[i.id].p; i.s = r.prices[i.id].s; } }); save(cc); paint();
          }
          msg.textContent = r.error || 'Er ging iets mis. Probeer het opnieuw.'; btn.disabled = !load().length;
        }).catch(function () { msg.textContent = 'Geen verbinding. Probeer het opnieuw.'; btn.disabled = false; });
    });
  }

  // --- Bedankpagina: status ophalen, winkelwagen pas leegmaken als betaald ---
  var th = $('thanks');
  if (th) {
    var tok = (location.search.match(/[?&]o=([a-f0-9]{32})/) || [])[1], tries = 0;
    var poll = function () {
      if (!tok) { th.textContent = 'Geen bestelling gevonden.'; return; }
      fetch('/status.php?o=' + tok).then(function (r) { return r.json(); }).then(function (r) {
        if (r.status === 'paid') { save([]); th.textContent = 'Bedankt! Je betaling is ontvangen. Je krijgt een bevestiging per e-mail.'; }
        else if (r.status === 'open' && tries++ < 12) { th.textContent = 'We wachten op de bevestiging van je betaling...'; setTimeout(poll, 2500); }
        else if (r.status === 'open') { th.textContent = 'De betaling is nog niet bevestigd. Zodra die binnen is, krijg je een e-mail. Je winkelwagen blijft bewaard.'; }
        else { th.textContent = 'De betaling is niet gelukt of geannuleerd. Je winkelwagen is bewaard; je kunt het opnieuw proberen.'; }
      }).catch(function () { th.textContent = 'Status niet op te halen. Controleer je e-mail voor de bevestiging.'; });
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
      fetch('/search.php?q=' + encodeURIComponent(q) + '&page=' + page).then(function (r) { return r.json(); }).then(function (r) {
        if (!r.ok) { info.textContent = r.error || 'Zoeken is nu niet mogelijk.'; return; }
        r.items.forEach(function (b) { res.appendChild(card(b)); });
        info.textContent = r.total ? r.total + (r.total === 1 ? ' boek gevonden' : ' boeken gevonden') : 'Geen boeken gevonden.';
        more.hidden = page * r.per_page >= r.total;
      }).catch(function () { info.textContent = 'Zoeken is nu niet mogelijk.'; });
    };
    if (q.length >= 2) { info.textContent = 'Zoeken...'; run(); more.addEventListener('click', function () { page++; run(); }); }
    else info.textContent = 'Vul minstens twee tekens in.';
  }
  badge();
})();
