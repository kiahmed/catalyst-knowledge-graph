// Firebase Authentication gate for the deployed frontend.
//
// The site requires sign-in. This module loads the Firebase Web SDK on
// demand, renders a custom sign-in modal, gates the app behind it, and
// upserts a users/{uid} profile doc in Firestore on every sign-in — so the
// user base accrues for later follow / entity-relation features.
//
// Sign-in methods: Google, email+password, and phone (SMS code). X and
// GitHub are coded for but not rendered — add them to SOCIAL once their
// OAuth apps exist and the providers are enabled in the Firebase Console.
//
// Local dev never calls this (no firebase-config.js → index.html reads
// cards.json directly), so the SDK is fetched only in prod.
//
// API:  RoboticsAuth.requireSignIn(firebaseConfig, onSignedIn)
//         → renders the gate immediately; calls onSignedIn(user) once, when authed.
//       RoboticsAuth.deferredGate(firebaseConfig, onSignedIn)
//         → lets anonymous visitors browse first; trips the gate after
//           GATE_DELAY_MS or on the GATE_CARD_OPENS'th card open
//           (whichever comes first), then behaves like requireSignIn.
//           Once tripped, the flag persists (localStorage) so the same
//           browser gates immediately on return visits.
//       RoboticsAuth.noteCardOpen()
//         → call on every card expand; feeds the deferred-gate counter.
(function () {
  'use strict';

  var SDK_VERSION = '10.14.1';
  var SDK_BASE = 'https://www.gstatic.com/firebasejs/' + SDK_VERSION + '/';

  // ── Deferred-gate tuning ───────────────────────────────────────────
  var GATE_DELAY_MS = 60 * 1000;  // trip N ms after first paint…
  var GATE_CARD_OPENS = 2;        // …or on the Nth card open, if sooner
  var GATE_KEY = 'rauth.gateTripped';  // localStorage: '1' once tripped

  // Social providers to render, in order. X ('twitter') + GitHub each need
  // an external OAuth app; add them here once enabled in the Console.
  var SOCIAL = ['google'];

  // ── Phase 2: shared .arboryx.ai session cookie (cross-subdomain SSO) ──
  // robotics.arboryx.ai exposes the arboryx-auth function first-party under
  // /__session/** (Firebase Hosting rewrite). Minting the cookie on sign-in
  // lets the apex (arboryx.ai) auto-detect this session, and vice-versa.
  var SESSION_BASE = '/__session';
  function sessionFetch(path, opts) {
    opts = opts || {};
    opts.credentials = 'include';   // send/receive the .arboryx.ai cookie
    return fetch(SESSION_BASE + path, opts);
  }
  function sessionLogin(user) {
    return user.getIdToken().then(function (idToken) {
      return sessionFetch('/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ idToken: idToken }),
      });
    }).catch(function (e) { console.warn('[RoboticsAuth] session login failed:', e); });
  }
  function sessionLogout() {
    return sessionFetch('/logout', {
      method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' },
    }).catch(function () {});
  }

  // ── SDK loading ────────────────────────────────────────────────────
  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error('failed to load ' + src)); };
      document.head.appendChild(s);
    });
  }

  function loadSdk() {
    if (window.firebase && firebase.auth && firebase.firestore) {
      return Promise.resolve();
    }
    return loadScript(SDK_BASE + 'firebase-app-compat.js').then(function () {
      return Promise.all([
        loadScript(SDK_BASE + 'firebase-auth-compat.js'),
        loadScript(SDK_BASE + 'firebase-firestore-compat.js'),
      ]);
    });
  }

  // ── Provider icons + labels ────────────────────────────────────────
  var ICONS = {
    google:
      '<svg viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.34A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.97 10.72A5.4 5.4 0 0 1 3.68 9c0-.6.1-1.18.29-1.72V4.94H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.06l3.01-2.34z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58A9 9 0 0 0 .96 4.94l3.01 2.34C4.68 5.16 6.66 3.58 9 3.58z"/></svg>',
    twitter:
      '<svg viewBox="0 0 24 24"><path fill="#000" d="M18.24 2.25h3.31l-7.23 8.26 8.5 11.24h-6.66l-5.21-6.82-5.97 6.82H1.68l7.73-8.84L1.25 2.25h6.83l4.71 6.23 5.45-6.23zm-1.16 17.52h1.83L7.08 4.13H5.12l11.96 15.64z"/></svg>',
    github:
      '<svg viewBox="0 0 24 24"><path fill="#1c1917" d="M12 .5a12 12 0 0 0-3.79 23.4c.6.1.82-.26.82-.58v-2.2c-3.34.72-4.04-1.6-4.04-1.6-.55-1.4-1.34-1.77-1.34-1.77-1.09-.74.08-.73.08-.73 1.2.09 1.84 1.24 1.84 1.24 1.07 1.84 2.81 1.3 3.5 1 .1-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.13-.3-.54-1.52.11-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.65 1.66.24 2.88.12 3.18.77.84 1.23 1.91 1.23 3.22 0 4.61-2.8 5.62-5.48 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.69.83.57A12 12 0 0 0 12 .5z"/></svg>',
  };
  var LABELS = { google: 'Google', twitter: 'X', github: 'GitHub' };

  // ── Gate markup ────────────────────────────────────────────────────
  function gateHTML() {
    var social = '';
    SOCIAL.forEach(function (id) {
      social +=
        '<button type="button" class="rauth-provider" data-provider="' + id + '">' +
        ICONS[id] + '<span>Continue with ' + LABELS[id] + '</span></button>';
    });
    return '' +
      '<div class="rauth-card" role="dialog" aria-modal="true" aria-label="Sign in">' +
        '<div class="rauth-brand">' +
          '<span class="rauth-logo">A</span>' +
          '<span>Robotics module <span class="rauth-brand-sub">· Arboryx</span></span>' +
        '</div>' +
        '<h1 class="rauth-title">Sign in to continue</h1>' +
        '<p class="rauth-sub">Sign in to explore the Robotics catalyst graph.</p>' +
        '<div class="rauth-providers">' + social + '</div>' +
        '<div class="rauth-divider"><span>or</span></div>' +
        '<form class="rauth-form" id="rauthForm" novalidate>' +
          '<div id="rauthEmailFields">' +
            '<input type="email" id="rauthEmail" placeholder="Email" autocomplete="email">' +
            '<input type="password" id="rauthPassword" placeholder="Password" autocomplete="current-password">' +
          '</div>' +
          '<div id="rauthPhoneFields" hidden>' +
            '<input type="tel" id="rauthPhone" placeholder="Phone number — e.g. +15555550123" autocomplete="tel">' +
          '</div>' +
          '<div id="rauthCodeFields" hidden>' +
            '<input type="text" id="rauthCode" placeholder="6-digit code" inputmode="numeric" autocomplete="one-time-code">' +
          '</div>' +
          '<button type="submit" class="rauth-submit" id="rauthSubmit">Sign in</button>' +
        '</form>' +
        '<div id="rauthRecaptcha"></div>' +
        '<div class="rauth-msg" id="rauthMsg" role="status"></div>' +
        '<div class="rauth-links" id="rauthEmailLinks">' +
          '<button type="button" class="rauth-link" id="rauthForgot">Forgot password?</button>' +
          '<button type="button" class="rauth-link" id="rauthToggle">Create an account</button>' +
        '</div>' +
        '<div class="rauth-links single">' +
          '<button type="button" class="rauth-link" id="rauthMethodLink">Use a phone number instead</button>' +
        '</div>' +
      '</div>';
  }

  // ── Error mapping ──────────────────────────────────────────────────
  function friendlyError(e) {
    var map = {
      'auth/invalid-email': 'That email address looks invalid.',
      'auth/user-not-found': 'No account with that email — create one below.',
      'auth/wrong-password': 'Incorrect password.',
      'auth/invalid-credential': 'Incorrect email or password.',
      'auth/email-already-in-use': 'An account with that email already exists — sign in instead.',
      'auth/weak-password': 'Password must be at least 6 characters.',
      'auth/popup-closed-by-user': 'Sign-in window closed before finishing.',
      'auth/cancelled-popup-request': 'Sign-in already in progress.',
      'auth/popup-blocked': 'Your browser blocked the sign-in popup — allow popups and retry.',
      'auth/account-exists-with-different-credential':
        'That account is already registered with a different sign-in method.',
      'auth/operation-not-allowed': 'That sign-in method is not enabled yet.',
      'auth/network-request-failed': 'Network error — check your connection.',
      'auth/too-many-requests': 'Too many attempts — wait a minute and retry.',
      'auth/invalid-phone-number': 'Enter the number in international format, e.g. +15555550123.',
      'auth/missing-phone-number': 'Enter a phone number.',
      'auth/quota-exceeded': 'SMS quota exceeded — try again later.',
      'auth/invalid-verification-code': 'That code is incorrect — check and retry.',
      'auth/code-expired': 'That code expired — request a new one.',
      'auth/captcha-check-failed': 'reCAPTCHA check failed — reload and retry.',
    };
    return (e && map[e.code]) || (e && e.message) || 'Sign-in failed. Please try again.';
  }

  // ── Controller state ───────────────────────────────────────────────
  var els = {};               // cached gate elements
  var mode = 'signin';        // 'signin' | 'signup' | 'phone'
  var phoneStep = 'number';   // within phone mode: 'number' | 'code'
  var recaptcha = null;       // firebase.auth.RecaptchaVerifier
  var confirmation = null;    // result of signInWithPhoneNumber
  var signaled = false;

  // Deferred-gate state
  var deferred = false;       // running via deferredGate()
  var tripped = false;        // gate has been (or must be) shown
  var authKnown = false;      // onAuthStateChanged has fired at least once
  var currentUser = null;     // latest auth state
  var cardOpens = 0;          // expands since load (deferred mode only)
  var gateTimer = null;

  function showMsg(text, kind) {
    els.msg.textContent = text || '';
    els.msg.className = 'rauth-msg' + (text ? ' ' + (kind || 'info') : '');
  }

  function setBusy(busy) {
    var btns = els.gate.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) btns[i].disabled = busy;
  }

  // Show/hide field groups + relabel controls for the current mode.
  function applyMode() {
    var emailMode = (mode === 'signin' || mode === 'signup');
    els.emailFields.hidden = !emailMode;
    els.phoneFields.hidden = !(mode === 'phone' && phoneStep === 'number');
    els.codeFields.hidden  = !(mode === 'phone' && phoneStep === 'code');
    els.emailLinks.hidden  = !emailMode;

    if (mode === 'signup')      els.submit.textContent = 'Create account';
    else if (mode === 'signin') els.submit.textContent = 'Sign in';
    else els.submit.textContent = (phoneStep === 'code') ? 'Verify code' : 'Send code';

    els.toggle.textContent = (mode === 'signup')
      ? 'Have an account? Sign in' : 'Create an account';
    els.forgot.hidden = (mode === 'signup');
    els.password.setAttribute('autocomplete',
      mode === 'signup' ? 'new-password' : 'current-password');
    els.methodLink.textContent = emailMode
      ? 'Use a phone number instead' : 'Use email instead';
    showMsg('', '');
  }

  function setMode(m) {
    mode = m;
    if (m !== 'phone') { phoneStep = 'number'; confirmation = null; }
    applyMode();
  }

  // ── Social (popup) sign-in ─────────────────────────────────────────
  function providerFor(id) {
    if (id === 'google')  return new firebase.auth.GoogleAuthProvider();
    if (id === 'twitter') return new firebase.auth.TwitterAuthProvider();
    if (id === 'github')  return new firebase.auth.GithubAuthProvider();
    return null;
  }

  function signInWithProvider(id) {
    var p = providerFor(id);
    if (!p) return;
    setBusy(true);
    showMsg('', '');
    firebase.auth().signInWithPopup(p)
      .catch(function (e) { showMsg(friendlyError(e), 'error'); })
      .then(function () { setBusy(false); });
  }

  // ── Email/password sign-in ─────────────────────────────────────────
  function submitEmail() {
    var email = els.email.value.trim();
    var pw = els.password.value;
    if (!email || !pw) { showMsg('Enter your email and password.', 'error'); return; }
    setBusy(true);
    showMsg('', '');
    var auth = firebase.auth();
    var op = (mode === 'signup')
      ? auth.createUserWithEmailAndPassword(email, pw)
      : auth.signInWithEmailAndPassword(email, pw);
    op.catch(function (e) { showMsg(friendlyError(e), 'error'); })
      .then(function () { setBusy(false); });
  }

  function forgotPassword() {
    var email = els.email.value.trim();
    if (!email) {
      showMsg('Enter your email above, then click “Forgot password?”.', 'info');
      return;
    }
    setBusy(true);
    firebase.auth().sendPasswordResetEmail(email)
      .then(function () { showMsg('Password reset email sent to ' + email + '.', 'info'); })
      .catch(function (e) { showMsg(friendlyError(e), 'error'); })
      .then(function () { setBusy(false); });
  }

  // ── Phone sign-in (SMS code) ───────────────────────────────────────
  function resetRecaptcha() {
    if (recaptcha) {
      try { recaptcha.clear(); } catch (_) {}
      recaptcha = null;
    }
  }

  function sendCode() {
    var phone = els.phone.value.trim();
    if (!phone) { showMsg('Enter a phone number.', 'error'); return; }
    setBusy(true);
    showMsg('Sending code…', 'info');
    // Invisible reCAPTCHA — required by Firebase before sending an SMS.
    if (!recaptcha) {
      recaptcha = new firebase.auth.RecaptchaVerifier('rauthRecaptcha', { size: 'invisible' });
    }
    firebase.auth().signInWithPhoneNumber(phone, recaptcha)
      .then(function (result) {
        confirmation = result;
        phoneStep = 'code';
        applyMode();
        showMsg('Code sent to ' + phone + '.', 'info');
        setBusy(false);
        els.code.focus();
      })
      .catch(function (e) {
        resetRecaptcha();          // a used/failed verifier can't be reused
        showMsg(friendlyError(e), 'error');
        setBusy(false);
      });
  }

  function verifyCode() {
    if (!confirmation) { showMsg('Request a code first.', 'error'); return; }
    var code = els.code.value.trim();
    if (!code) { showMsg('Enter the 6-digit code.', 'error'); return; }
    setBusy(true);
    showMsg('Verifying…', 'info');
    confirmation.confirm(code)
      .catch(function (e) {
        showMsg(friendlyError(e), 'error');
        setBusy(false);
      });
    // success → onAuthStateChanged hides the gate.
  }

  // ── Form dispatch ──────────────────────────────────────────────────
  function onSubmit(ev) {
    ev.preventDefault();
    if (mode === 'phone') {
      if (phoneStep === 'code') verifyCode();
      else sendCode();
    } else {
      submitEmail();
    }
  }

  // ── Gate construction ──────────────────────────────────────────────
  function buildGate() {
    var gate = document.createElement('div');
    gate.className = 'rauth-gate';
    gate.id = 'rauthGate';
    gate.innerHTML = gateHTML();
    document.body.appendChild(gate);

    els.gate = gate;
    els.msg = gate.querySelector('#rauthMsg');
    els.email = gate.querySelector('#rauthEmail');
    els.password = gate.querySelector('#rauthPassword');
    els.phone = gate.querySelector('#rauthPhone');
    els.code = gate.querySelector('#rauthCode');
    els.emailFields = gate.querySelector('#rauthEmailFields');
    els.phoneFields = gate.querySelector('#rauthPhoneFields');
    els.codeFields = gate.querySelector('#rauthCodeFields');
    els.submit = gate.querySelector('#rauthSubmit');
    els.toggle = gate.querySelector('#rauthToggle');
    els.forgot = gate.querySelector('#rauthForgot');
    els.emailLinks = gate.querySelector('#rauthEmailLinks');
    els.methodLink = gate.querySelector('#rauthMethodLink');

    var provBtns = gate.querySelectorAll('.rauth-provider');
    for (var i = 0; i < provBtns.length; i++) {
      (function (btn) {
        btn.addEventListener('click', function () {
          signInWithProvider(btn.getAttribute('data-provider'));
        });
      })(provBtns[i]);
    }
    gate.querySelector('#rauthForm').addEventListener('submit', onSubmit);
    els.toggle.addEventListener('click', function () {
      setMode(mode === 'signin' ? 'signup' : 'signin');
    });
    els.forgot.addEventListener('click', forgotPassword);
    els.methodLink.addEventListener('click', function () {
      setMode(mode === 'phone' ? 'signin' : 'phone');
    });

    setMode('signin');
  }

  function showGate() { if (els.gate) els.gate.hidden = false; }
  function hideGate() { if (els.gate) els.gate.hidden = true; }

  // ── Signed-in chip ─────────────────────────────────────────────────
  function showUserChip(user) {
    var chip = document.getElementById('rauthUserChip');
    if (!chip) {
      chip = document.createElement('div');
      chip.className = 'rauth-userchip';
      chip.id = 'rauthUserChip';
      chip.innerHTML =
        '<span class="rauth-userchip-name" id="rauthUserName"></span>' +
        '<button type="button" class="rauth-signout" id="rauthSignout">Sign out</button>';
      document.body.appendChild(chip);
      chip.querySelector('#rauthSignout').addEventListener('click', function () {
        sessionLogout().then(function () { firebase.auth().signOut(); });
      });
    }
    chip.querySelector('#rauthUserName').textContent =
      user.displayName || user.email || user.phoneNumber || 'Signed in';
    chip.hidden = false;
  }
  function hideUserChip() {
    var chip = document.getElementById('rauthUserChip');
    if (chip) chip.hidden = true;
  }

  // ── users/{uid} profile upsert ─────────────────────────────────────
  function upsertProfile(user) {
    var ref = firebase.firestore().collection('users').doc(user.uid);
    var now = firebase.firestore.FieldValue.serverTimestamp();
    var providerId =
      (user.providerData && user.providerData[0] && user.providerData[0].providerId) ||
      'password';
    var data = {
      uid: user.uid,
      email: user.email || null,
      phoneNumber: user.phoneNumber || null,
      displayName: user.displayName || null,
      photoURL: user.photoURL || null,
      provider: providerId,
      lastSeenAt: now,
    };
    return ref.get().catch(function () { return null; }).then(function (snap) {
      if (!snap || !snap.exists) data.createdAt = now;
      return ref.set(data, { merge: true });
    });
  }

  // ── Deferred-gate triggers ─────────────────────────────────────────
  function gateWasTripped() {
    try { return localStorage.getItem(GATE_KEY) === '1'; } catch (_) { return false; }
  }
  function markTripped() {
    try { localStorage.setItem(GATE_KEY, '1'); } catch (_) {}
  }

  // Trip the gate. No-op once signed in; if the auth state is still
  // unknown, only the flag is set — the onAuthStateChanged handler shows
  // the gate if the state resolves to signed-out.
  function tripGate() {
    if (tripped) return;
    if (authKnown && currentUser) return;   // signed in — never gate
    tripped = true;
    markTripped();
    if (authKnown && !currentUser) {
      if (!els.gate) buildGate();
      showGate();
    }
  }

  function armGateTimer() {
    if (gateTimer !== null || tripped) return;
    gateTimer = setTimeout(function () { gateTimer = null; tripGate(); }, GATE_DELAY_MS);
  }

  function noteCardOpen() {
    if (!deferred || tripped) return;
    cardOpens += 1;
    if (cardOpens >= GATE_CARD_OPENS) tripGate();
  }

  // ── Public entry points ────────────────────────────────────────────
  function start(cfg, onSignedIn) {
    var done = function (u) { if (!signaled) { signaled = true; onSignedIn(u); } };

    loadSdk().then(function () {
      if (!firebase.apps.length) {
        firebase.initializeApp({
          apiKey: cfg.apiKey,
          authDomain: cfg.authDomain,
          projectId: cfg.projectId,
          appId: cfg.appId,
        });
      }
      // Immediate mode (or an already-tripped return visit): gate is
      // visible until onAuthStateChanged decides. Deferred + untripped:
      // no gate yet — the visitor browses until a trigger fires.
      if (!deferred || tripped) buildGate();
      firebase.auth().onAuthStateChanged(function (user) {
        currentUser = user;
        authKnown = true;
        if (user) {
          hideGate();
          showUserChip(user);
          upsertProfile(user).catch(function (e) {
            console.warn('user profile upsert failed:', e);
          });
          sessionLogin(user);  // Phase 2: mint the shared .arboryx.ai cookie
          done(user);          // load catalysts (once)
        } else {
          hideUserChip();
          if (!deferred || tripped) {
            if (!els.gate) buildGate();
            showGate();
          } else {
            // Anonymous browse window — start (or restart, after a
            // sign-out) the countdown.
            armGateTimer();
          }
        }
      });
    }).catch(function (e) {
      // SDK/init failure — don't hard-lock the page; let it try (and fall
      // back to its offline preview if the Firestore read also fails).
      console.error('auth init failed:', e);
      done(null);
    });
  }

  function requireSignIn(cfg, onSignedIn) {
    start(cfg, onSignedIn);
  }

  function deferredGate(cfg, onSignedIn) {
    deferred = true;
    tripped = gateWasTripped();   // return visit → gate immediately
    if (!tripped) armGateTimer(); // clock starts at first paint, not SDK-ready
    start(cfg, onSignedIn);
  }

  window.RoboticsAuth = {
    requireSignIn: requireSignIn,
    deferredGate: deferredGate,
    noteCardOpen: noteCardOpen,
  };
})();
