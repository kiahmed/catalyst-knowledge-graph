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

    return Promise.all([graphRef.get(), itemsRef.get()]).then(function (results) {
      var graphSnap = results[0];
      var itemsSnap = results[1];
      if (!graphSnap.exists) {
        throw new Error('graph doc missing in ' + cfg.collection +
                        ' — run make deploy / firestore-sync first');
      }
      var payload = graphSnap.data() || {};
      payload.cards = itemsSnap.docs.map(function (d) { return d.data(); });
      return payload;
    });
  }

  window.RoboticsFirestore = { load: load };
})();
