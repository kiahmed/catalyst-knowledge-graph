// Firestore data source for the DEPLOYED frontend.
//
// The deployed page reads catalysts + graph live from Firestore
// (CKG-<sector>) instead of a static cards.json. Local dev still uses
// cards.json — index.html picks the source by whether window.FIREBASE_CONFIG
// is present (deploy.sh generates firebase-config.js; local dev has none).
//
// Auth + Firebase SDK init are owned by auth.js — by the time load() runs
// the user is already signed in and `firebase` is initialised. This module
// just performs the two reads and reassembles the cards.json payload shape:
//   CKG-<sector>/graph/sectors/<sector>   → schema_version, stats, graph, ...
//   CKG-<sector>/catalysts/items/*        → one doc per card
// (mirrors src/firestore_export.py's write layout).
(function () {
  'use strict';

  // Returns Promise<payload> — same shape as cards.json.
  function load(cfg) {
    if (!window.firebase || !firebase.firestore) {
      return Promise.reject(new Error('Firebase SDK not initialised'));
    }
    var db = firebase.firestore();
    var graphRef = db.collection(cfg.collection)
      .doc(cfg.graphSubpath)               // "graph"
      .collection('sectors')
      .doc(cfg.sector);                    // "Robotics"
    var itemsRef = db.collection(cfg.collection)
      .doc(cfg.catalystsSubpath)           // "catalysts"
      .collection('items');

    // Full-history node positions (own doc; may not exist on older exports).
    var layoutRef = db.collection(cfg.collection).doc(cfg.graphSubpath)
      .collection('layout').doc(cfg.sector);

    var baseRef = db.collection(cfg.collection).doc(cfg.catalystsSubpath);

    return Promise.all([graphRef.get(), itemsRef.get(),
                        layoutRef.get().catch(function () { return null; }),
                        baseRef.get().catch(function () { return null; })]).then(function (results) {
      var graphSnap = results[0];
      var itemsSnap = results[1];
      var layoutSnap = results[2];
      var baseSnap = results[3];
      if (!graphSnap.exists) {
        throw new Error('graph doc missing in ' + cfg.collection +
                        ' — run make deploy / firestore-sync first');
      }
      var payload = graphSnap.data() || {};
      payload.cards = itemsSnap.docs.map(function (d) { return d.data(); });
      payload.layout = (layoutSnap && layoutSnap.exists) ? layoutSnap.data() : null;
      // What this page has seen: the data version + the newest card write (see checkUpdates).
      var base = (baseSnap && baseSnap.exists) ? (baseSnap.data() || {}) : {};
      payload._sync = { version: base.version || null, synced: maxSynced(payload.cards),
                        deleted: (base.deleted_ids || []).length };
      return payload;
    });
  }

  function maxSynced(cards) {
    var m = null;
    (cards || []).forEach(function (c) { if (c.synced_at && (!m || c.synced_at > m)) m = c.synced_at; });
    return m;
  }

  // Silent refresh (docs/ticket-frontend-silent-refresh.md). One doc read when nothing changed: the
  // catalysts parent doc's `version` moves only when the backend wrote cards (ingest export or an admin
  // save). When it moved: fetch ONLY the cards written after the newest one this page holds, plus the
  // ids deleted since. Resolves null (nothing new) or {version, changed: [card docs], deleted: [ids]}.
  // Delta window: re-fetch cards stamped up to SYNC_OVERLAP_MS before the newest one this page holds. Stamps
  // are taken before a write commits, so an ingest export (earlier stamp, still writing) can finish AFTER an
  // admin save (later stamp) was synced; the overlap still catches its last chunks. Re-fetching is harmless.
  var SYNC_OVERLAP_MS = 30 * 60 * 1000;

  function checkUpdates(cfg, since) {
    if (!window.firebase || !firebase.firestore) {
      return Promise.reject(new Error('Firebase SDK not initialised'));
    }
    var db = firebase.firestore();
    var baseRef = db.collection(cfg.collection).doc(cfg.catalystsSubpath);
    return baseRef.get().then(function (snap) {
      var meta = snap.exists ? (snap.data() || {}) : {};
      if (!meta.version || meta.version === since.version) return null;
      var items = baseRef.collection('items');
      // No synced_at seen yet (cards exported before this feature): everything is "changed".
      var from = since.synced
        ? new Date(new Date(since.synced).getTime() - SYNC_OVERLAP_MS).toISOString() : null;
      var q = from ? items.where('synced_at', '>=', from) : items;
      return q.get().then(function (qs) {
        var deleted = meta.deleted_ids || [];
        return { version: meta.version, changed: qs.docs.map(function (d) { return d.data(); }),
                 deleted: deleted.slice(since.deleted || 0), deletedTotal: deleted.length,
                 full: !since.synced };
      });
    });
  }

  window.RoboticsFirestore = { load: load, checkUpdates: checkUpdates, maxSynced: maxSynced };
})();
