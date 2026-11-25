// Builds the Bulk Clock meal-alarm calendar. Shared by the page and the build script.
(function (root) {
  const pad = n => String(n).padStart(2, '0');
  function local(d) { return d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + 'T' + pad(d.getHours()) + pad(d.getMinutes()) + '00'; }
  // times: minutes after midnight (can exceed 1440), kcal: per meal, startYmd/untilYmd: 'YYYY-MM-DD'
  function buildIcs({ times, kcal, startYmd, untilYmd }) {
    const [y, m, d] = startYmd.split('-').map(Number);
    const n = times.length;
    const until = untilYmd.replace(/-/g, '') + 'T235900';
    const L = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Bulk Clock//EN', 'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:Bulk Clock'];
    times.forEach((t, i) => {
      const s = new Date(y, m - 1, d, 0, t);
      const e = new Date(s.getTime() + 20 * 60e3);
      L.push('BEGIN:VEVENT', `UID:bulkclock-meal-${i + 1}@bulkclock.app`, 'DTSTAMP:' + local(new Date(y, m - 1, d)) + 'Z',
        'DTSTART:' + local(s), 'DTEND:' + local(e), 'RRULE:FREQ=DAILY;UNTIL=' + until,
        `SUMMARY:Eat: meal ${i + 1} of ${n} (${kcal} kcal)`,
        `DESCRIPTION:Bulk Clock. ${kcal} kcal now. Log it in the app.`,
        'TRANSP:TRANSPARENT',
        'BEGIN:VALARM', 'ACTION:DISPLAY', `DESCRIPTION:Eat now: meal ${i + 1}`, 'TRIGGER:PT0M', 'END:VALARM',
        'END:VEVENT');
    });
    L.push('END:VCALENDAR');
    return L.join('\r\n') + '\r\n';
  }
  const DEFAULT = { times: [510, 655, 800, 945, 1090, 1235, 1380], kcal: 500, startYmd: '2026-11-25', untilYmd: '2026-12-12' };
  root.BulkIcs = { buildIcs, DEFAULT };
  if (typeof module !== 'undefined') module.exports = root.BulkIcs;
})(typeof globalThis !== 'undefined' ? globalThis : this);
