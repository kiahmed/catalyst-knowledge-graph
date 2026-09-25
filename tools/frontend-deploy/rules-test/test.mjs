import { readFileSync } from "node:fs";
import { initializeTestEnvironment, assertSucceeds, assertFails } from "@firebase/rules-unit-testing";
import { doc, getDoc, setDoc, updateDoc, deleteDoc, serverTimestamp } from "firebase/firestore";

const env = await initializeTestEnvironment({
  projectId: "demo-rules",
  firestore: { rules: readFileSync("firestore.rules", "utf8"), host: "127.0.0.1", port: 8080 },
});
let pass = 0, fail = 0;
async function t(name, fn) {
  try { await fn(); pass++; console.log("  ok  ", name); }
  catch (e) { fail++; console.log("  FAIL", name, "-", e.message.split("\n")[0]); }
}
async function seed() {
  await env.clearFirestore();
  await env.withSecurityRulesDisabled(async (c) => {
    const db = c.firestore();
    await setDoc(doc(db, "config/products/items/arboryx"), { displayName: "Arboryx", tier: 1 });
    await setDoc(doc(db, "config/products/items/robotics"), { displayName: "Robotics", tier: 2 });
    await setDoc(doc(db, "CKG-Robotics/catalysts/items/ROB-1"), { headline: "h" });
    await setDoc(doc(db, "findings/ROB-1"), { x: 1 });
    await setDoc(doc(db, "users/bob"), { uid: "bob" });
    await setDoc(doc(db, "users/legacy"), { uid: "legacy", products: { arboryx: { member: true } } });
    await setDoc(doc(db, "users/alice/products/arboryx"), { productId: "arboryx", tier: 1 });
  });
}
const alice = () => env.authenticatedContext("alice").firestore();
const anon = () => env.unauthenticatedContext().firestore();
// Robotics upsertProfile payload, verbatim shape from frontend/assets/auth.js
const profile = (uid) => ({ uid, email: "a@x.io", phoneNumber: null, displayName: "A",
  photoURL: null, provider: "google.com", lastSeenAt: serverTimestamp() });

await seed();
console.log("robotics (must keep working)");
await t("signed-in reads CKG-*", () => assertSucceeds(getDoc(doc(alice(), "CKG-Robotics/catalysts/items/ROB-1"))));
await t("anon cannot read CKG-*", () => assertFails(getDoc(doc(anon(), "CKG-Robotics/catalysts/items/ROB-1"))));
await t("findings denied to browsers", () => assertFails(getDoc(doc(alice(), "findings/ROB-1"))));
await t("upsertProfile create (merge)", () => assertSucceeds(setDoc(doc(alice(), "users/alice"), { ...profile("alice"), createdAt: serverTimestamp() }, { merge: true })));
await t("upsertProfile repeat sign-in (merge)", () => assertSucceeds(setDoc(doc(alice(), "users/alice"), profile("alice"), { merge: true })));
await t("read own profile", () => assertSucceeds(getDoc(doc(alice(), "users/alice"))));
await t("cannot read other's profile", () => assertFails(getDoc(doc(alice(), "users/bob"))));
await t("cannot write other's profile", () => assertFails(setDoc(doc(alice(), "users/bob"), profile("bob"), { merge: true })));
await t("uid field must match", () => assertFails(setDoc(doc(alice(), "users/alice"), { ...profile("alice"), uid: "bob" }, { merge: true })));
await t("no client delete", () => assertFails(deleteDoc(doc(alice(), "users/alice"))));
await t("legacy doc with products: upsertProfile still ok", () =>
  assertSucceeds(setDoc(doc(env.authenticatedContext("legacy").firestore(), "users/legacy"), profile("legacy"), { merge: true })));

console.log("fix #2: entitlement/products blocked on users/{uid}");
await seed();
await t("create with entitlement denied", () => assertFails(setDoc(doc(alice(), "users/alice"), { ...profile("alice"), entitlement: "pro" })));
await t("create with products denied", () => assertFails(setDoc(doc(alice(), "users/alice"), { ...profile("alice"), products: { arboryx: { member: true, access: true } } })));
await setDoc(doc(alice(), "users/alice"), profile("alice"));
await t("update adds entitlement denied", () => assertFails(updateDoc(doc(alice(), "users/alice"), { entitlement: "pro" })));
await t("update products.x.access denied", () => assertFails(updateDoc(doc(alice(), "users/alice"), { "products.other.access": true })));

console.log("fix #1: products/{id}.tier locked to catalog");
await seed();
const sub = (id) => doc(alice(), `users/alice/products/${id}`);
await t("read own membership", () => assertSucceeds(getDoc(sub("arboryx"))));
await t("update with catalog tier ok", () => assertSucceeds(updateDoc(sub("arboryx"), { lastSeenAt: serverTimestamp() })));
await t("update tier escalation denied", () => assertFails(updateDoc(sub("arboryx"), { tier: 9 })));
await t("update entitlement denied", () => assertFails(updateDoc(sub("arboryx"), { entitlement: "pro" })));
await t("update productId swap denied", () => assertFails(updateDoc(sub("arboryx"), { productId: "other" })));
await env.withSecurityRulesDisabled((c) => deleteDoc(doc(c.firestore(), "users/alice/products/arboryx")));
await t("create with catalog tier ok", () => assertSucceeds(setDoc(sub("arboryx"), { productId: "arboryx", tier: 1 })));
await env.withSecurityRulesDisabled((c) => deleteDoc(doc(c.firestore(), "users/alice/products/arboryx")));
await t("create with wrong tier denied", () => assertFails(setDoc(sub("arboryx"), { productId: "arboryx", tier: 5 })));
await t("create with entitlement denied", () => assertFails(setDoc(sub("arboryx"), { productId: "arboryx", tier: 1, entitlement: "pro" })));
await t("create mismatched productId denied", () => assertFails(setDoc(sub("arboryx"), { productId: "x", tier: 1 })));
await t("create unknown product denied", () => assertFails(setDoc(sub("bogus"), { productId: "bogus", tier: 1 })));
await t("other user's subdoc denied", () => assertFails(getDoc(doc(alice(), "users/bob/products/arboryx"))));
await t("no subdoc delete", () => assertFails(deleteDoc(sub("arboryx"))));

console.log("robotics membership (auth.js upsertMembership)");
await seed();
const rob = () => doc(alice(), "users/alice/products/robotics");
const robData = (extra) => ({ productId: "robotics", tier: 2, lastSeenAt: serverTimestamp(), ...extra });
const first = { joinedAt: serverTimestamp(), joinedVia: "robotics" };
await t("first sign-in creates tier-2 robotics membership", () => assertSucceeds(setDoc(rob(), robData(first), { merge: true })));
await t("first sign-in creates tier-1 arboryx base membership", () => assertSucceeds(setDoc(doc(alice(), "users/alice/products/arboryx"),
  { productId: "arboryx", tier: 1, lastSeenAt: serverTimestamp(), ...first }, { merge: true })));
await t("repeat sign-in merge update ok", () => assertSucceeds(setDoc(rob(), robData(), { merge: true })));
await t("tier 1 on robotics denied", () => assertFails(setDoc(rob(), robData({ tier: 1 }), { merge: true })));

console.log("catalog");
await t("anon reads catalog", () => assertSucceeds(getDoc(doc(anon(), "config/products/items/arboryx"))));
await t("client cannot write catalog", () => assertFails(setDoc(doc(alice(), "config/products/items/arboryx"), { tier: 9 })));

await env.cleanup();
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
