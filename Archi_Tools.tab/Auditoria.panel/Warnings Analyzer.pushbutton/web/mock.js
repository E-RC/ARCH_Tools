// Datos de ejemplo para previsualizar la UI en un navegador común.
// Dentro de Revit no se usa: app.js solo llama a mockCall si no hay chrome.webview.
(function () {
  const types = [
    ['There are identical instances in the same place. This will result in double counting in schedules.', 6, 'Furniture'],
    ['Highlighted walls overlap. One of them may be ignored when Revit finds room boundaries. Use Cut Geometry to embed one wall within the other.', 9, 'Walls'],
    ['Room separation line(s) overlap. One of them may be ignored when Revit finds room boundaries. Delete one of the lines.', 7, 'Room Separation'],
    ['Room is not in a properly enclosed region', 4, 'Rooms'],
    ['Line in Sketch is slightly off axis and may cause inaccuracies.', 12, 'Lines'],
    ['Elements have duplicate "Mark" values.', 31, 'Doors'],
    ['Highlighted elements are joined but do not intersect.', 5, 'Floors'],
    ['Stair top end exceeds or cannot reach the top elevation of the stair.', 2, 'Stairs'],
  ];
  const levels = ['P1', 'P2', 'P3', 'S1'];
  const elements = {}, warnings = [];
  let id = 300100;
  types.forEach(([d, n, cat]) => {
    for (let i = 0; i < n; i++) {
      const f = [id++, id++];
      f.forEach((x, k) => { elements[x] = { cat, name: cat + ' tipo ' + (k + 1), level: levels[(x >> 1) % 4], view: '' }; });
      warnings.push({ d, g: '0', f, a: [] });
    }
  });
  const history = [
    { date: '2026-10-01', total: 120, open: 118, high: 30 },
    { date: '2026-10-08', total: 101, open: 98, high: 24 },
    { date: '2026-10-15', total: 84, open: 80, high: 19 },
  ];
  window.mockCall = (action, payload) => new Promise(resolve => setTimeout(() => {
    if (action === 'scan') {
      resolve({ doc: { title: 'Modelo de ejemplo', key: 'demo', workshared: false, elementCount: 18450, view: 'P1', user: 'demo' },
        elements, warnings, history, accepted: [], scannedAt: new Date().toISOString().slice(0, 16).replace('T', ' ') });
    } else if (action === 'snapshot') {
      resolve(history.concat([Object.assign({ date: new Date().toISOString().slice(0, 10) }, payload)]));
    } else if (action === 'accept') {
      resolve(payload.accepted ? payload.keys : []);
    } else if (action === 'view_ids') {
      resolve({ ids: payload.ids.filter((x, i) => i % 2), view: 'P1' });
    } else {
      resolve({ count: (payload.ids || []).length, view: 'P1', deleted: [], skipped: [], partial: 0, path: null });
    }
  }, 150));
})();
