// Munbyn BLE capture snippet: paste into Chrome DevTools on https://editor.munbyn.com/create
// BEFORE clicking Connect. It only records traffic. It sends nothing and changes nothing.
//
// Steps: open the editor, open DevTools (Cmd+Opt+J), paste this and press Enter. Then connect to
// the RW403B, print one label, wait for it to come out, and run   __munbynCap.save()
// That downloads munbyn-ble-capture.json. Decode it with
//   python3 tests/fixtures/ble/decode_capture.py munbyn-ble-capture.json
// If the editor was already connected, disconnect and reconnect after pasting, so that
// startNotifications() runs again through the wrapper.
//
// Our code (munbyn-print), not Munbyn's. See PLANS/BLE-PROTOCOL.md, "Capture plan".
(() => {
  if (window.__munbynCap) { console.log('munbyn capture already installed'); return; }
  const cap = (window.__munbynCap = { t0: performance.now(), log: [], chars: {} });
  const now = () => +(performance.now() - cap.t0).toFixed(1);
  const hex = (v) => {
    const u = v instanceof ArrayBuffer ? new Uint8Array(v) : new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
    return Array.from(u, (b) => b.toString(16).padStart(2, '0')).join('');
  };
  const PROPS = ['broadcast', 'read', 'writeWithoutResponse', 'write', 'notify', 'indicate',
    'authenticatedSignedWrites', 'reliableWrite', 'writableAuxiliaries'];
  const note = (ch) => {
    const u = ch.uuid;
    if (!cap.chars[u]) {
      cap.chars[u] = {
        device: ch.service && ch.service.device && ch.service.device.name,
        service: ch.service && ch.service.uuid,
        properties: Object.fromEntries(PROPS.map((k) => [k, !!(ch.properties && ch.properties[k])])),
      };
    }
    return u;
  };
  const P = BluetoothRemoteGATTCharacteristic.prototype;
  for (const m of ['writeValue', 'writeValueWithResponse', 'writeValueWithoutResponse']) {
    const orig = P[m];
    if (!orig) continue;
    P[m] = function (value) {
      const e = { t: now(), dir: 'write', method: m, char: note(this), hex: hex(value) };
      cap.log.push(e);
      const r = orig.apply(this, arguments);
      r.then(() => { e.doneT = now(); }, (err) => { e.error = String(err); });
      return r;
    };
  }
  const onNotify = (ev) => cap.log.push({ t: now(), dir: 'notify', char: note(ev.target), hex: hex(ev.target.value) });
  const origStart = P.startNotifications;
  P.startNotifications = function () {
    note(this);
    if (!this.__munbynCap) { this.addEventListener('characteristicvaluechanged', onNotify); this.__munbynCap = true; }
    return origStart.apply(this, arguments);
  };
  cap.save = (name = 'munbyn-ble-capture.json') => {
    const body = JSON.stringify({ capturedAt: new Date().toISOString(), userAgent: navigator.userAgent,
      page: location.href, chars: cap.chars, log: cap.log }, null, 1);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([body], { type: 'application/json' }));
    a.download = name;
    a.click();
    return `${cap.log.length} events saved to ${name}`;
  };
  console.log('munbyn capture installed: connect, print one label, then run __munbynCap.save()');
})();
