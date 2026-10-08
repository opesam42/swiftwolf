(() => {
  const customerId = window.SW_CUSTOMER_ID;
  const canvas = document.getElementById("ewma-band");
  const statusEl = document.getElementById("chart-status");
  const categorySelect = document.getElementById("category-select");
  const naira = new Intl.NumberFormat("en-NG", {
    style: "currency",
    currency: "NGN",
    maximumFractionDigits: 2,
  });

  const NOTICE_MS = 20000;
  const BAND_TWEEN_MS = 900;
  const PULSE_MS = 200;

  const decisionColor = {
    PROCEED: "#34d399",
    STEP_UP: "#f0b429",
    BLOCK: "#f07167",
  };

  const zoneFill = {
    1: { label: "1σ", color: "rgba(16, 185, 129, 0.35)" },
    2.5: { label: "2.5σ", color: "rgba(94, 200, 255, 0.28)" },
    3: { label: "3σ", color: "rgba(240, 180, 41, 0.28)" },
    4: { label: "4σ", color: "rgba(249, 115, 22, 0.28)" },
    5: { label: "5σ", color: "rgba(240, 113, 103, 0.32)" },
  };

  let category = window.SW_CATEGORY || "transfer";
  let chart;
  let latest = { points: [] };
  let lastFingerprint = "";
  let seenPointKey = null;
  let noticeUntil = 0;
  let pulseTimer = null;

  function fingerprint(payload) {
    const { latency_ms, ...rest } = payload;
    return JSON.stringify(rest);
  }

  function lastPointKey(payload) {
    const live = (payload.points || []).filter((point) => point.pending);
    const last = live[live.length - 1];
    if (!last) return "";
    return `${last.amount}|${last.decision}|${live.length}`;
  }

  function showStatus(message) {
    if (!statusEl) return;
    statusEl.hidden = !message;
    statusEl.textContent = message || "";
  }

  function paintLatency(ms) {
    document.getElementById("stat-latency").textContent = String(ms);
  }

  function paintBaseline(payload) {
    document.getElementById("stat-mu").textContent = naira.format(payload.ewma_avg);
    document.getElementById("stat-sigma").textContent = naira.format(payload.ewma_std);
    document.getElementById("stat-k").textContent = String(payload.k_active);
    document.getElementById("stat-k-max").textContent = String(payload.k_max);
  }

  function zoneLook(z) {
    return zoneFill[z] || { label: `${z}σ`, color: "rgba(148, 163, 184, 0.2)" };
  }

  function chartData(payload) {
    const points = payload.points || [];
    const zones = payload.zones || [];
    const count = Math.max(points.length, 2);
    const labels = Array.from({ length: count }, (_, i) => String(i + 1));
    const mu = Array(count).fill(payload.ewma_avg);
    const amounts = labels.map((_, i) => (points[i] ? points[i].amount : null));
    const colors = labels.map((_, i) => {
      const point = points[i];
      if (!point) return "transparent";
      if (point.pending) return decisionColor[point.decision] || "#f0b429";
      return decisionColor[point.decision] || "#8b9bb8";
    });
    const zoneSeries = zones.map((zone) => ({
      ...zone,
      data: Array(count).fill(zone.upper),
      ...zoneLook(zone.z),
    }));
    return { labels, mu, amounts, colors, zoneSeries };
  }

  function datasetsFrom(data) {
    const bands = [
      {
        label: "μ floor",
        data: data.mu,
        borderColor: "transparent",
        backgroundColor: "transparent",
        borderWidth: 0,
        pointRadius: 0,
        fill: false,
        tension: 0,
        order: 4,
      },
      ...data.zoneSeries.map((zone) => ({
        label: zone.label,
        data: zone.data,
        borderColor: "transparent",
        backgroundColor: zone.color,
        borderWidth: 0,
        pointRadius: 0,
        fill: "-1",
        tension: 0,
        order: 4,
      })),
    ];
    return [
      ...bands,
      {
        label: "EWMA mean (μ)",
        data: data.mu,
        borderColor: "#5ec8ff",
        backgroundColor: "#5ec8ff",
        borderWidth: 2,
        pointRadius: 0,
        fill: false,
        tension: 0,
        order: 2,
      },
      {
        label: "Payments",
        data: data.amounts,
        showLine: false,
        pointBackgroundColor: data.colors,
        pointBorderColor: data.colors,
        pointRadius: paymentRadii(data.amounts),
        pointHoverRadius: 8,
        fill: false,
        order: 1,
      },
    ];
  }

  function paymentRadii(amounts) {
    const points = latest.points || [];
    const last = amounts.length - 1;
    const pulsing = Date.now() < noticeUntil;
    const pulse = 6 + Math.round(5 * (0.5 + 0.5 * Math.sin(Date.now() / 140)));
    return amounts.map((amount, i) => {
      if (amount == null) return 0;
      if (pulsing && points[i] && points[i].pending && i === last) return pulse;
      return 5;
    });
  }

  function yMax(payload, amounts) {
    const zoneMax = Math.max(0, ...(payload.zones || []).map((zone) => zone.upper));
    const amountMax = Math.max(0, ...amounts.filter((n) => n != null));
    return Math.max(zoneMax, amountMax, payload.upper || 0);
  }

  function paymentsDataset() {
    if (!chart) return null;
    return chart.data.datasets[chart.data.datasets.length - 1];
  }

  function startNotice() {
    noticeUntil = Date.now() + NOTICE_MS;
    if (pulseTimer) return;
    pulseTimer = setInterval(tickPulse, PULSE_MS);
  }

  function stopNotice() {
    noticeUntil = 0;
    if (pulseTimer) {
      clearInterval(pulseTimer);
      pulseTimer = null;
    }
  }

  function tickPulse() {
    const payments = paymentsDataset();
    if (!payments) return;
    payments.pointRadius = paymentRadii(payments.data || []);
    chart.options.animation = { duration: 0 };
    chart.update("none");
    if (Date.now() >= noticeUntil) {
      stopNotice();
    }
  }

  function patchChart(payload, { animateBands }) {
    const data = chartData(payload);
    const suggestedMax = yMax(payload, data.amounts);
    const xTitle = `Settled ${payload.category} (oldest → newest), live on the right`;

    chart.data.labels = data.labels;
    chart.data.datasets[0].data = data.mu;
    data.zoneSeries.forEach((zone, index) => {
      chart.data.datasets[1 + index].data = zone.data;
    });
    const meanIndex = 1 + data.zoneSeries.length;
    chart.data.datasets[meanIndex].data = data.mu;
    const payments = chart.data.datasets[meanIndex + 1];
    payments.data = data.amounts;
    payments.pointBackgroundColor = data.colors;
    payments.pointBorderColor = data.colors;
    payments.pointRadius = paymentRadii(data.amounts);

    chart.options.scales.y.suggestedMax = suggestedMax;
    chart.options.scales.x.title.text = xTitle;

    if (animateBands) {
      chart.options.animation = {
        duration: BAND_TWEEN_MS,
        easing: "easeInOutQuad",
        onComplete() {
          chart.options.animation = { duration: 0 };
        },
      };
      chart.update();
      return;
    }

    chart.options.animation = { duration: 0 };
    chart.update("none");
  }

  function createChart(payload) {
    const data = chartData(payload);
    const suggestedMax = yMax(payload, data.amounts);
    const xTitle = `Settled ${payload.category} (oldest → newest), live on the right`;

    chart = new Chart(canvas, {
      type: "line",
      data: { labels: data.labels, datasets: datasetsFrom(data) },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 },
        interaction: { mode: "nearest", intersect: false },
        plugins: {
          filler: { propagate: false },
          legend: {
            labels: {
              color: "#8b9bb8",
              boxWidth: 12,
              filter: (item) => item.text !== "μ floor",
            },
          },
          tooltip: {
            callbacks: {
              label(ctx) {
                if (ctx.dataset.label !== "Payments") {
                  return `${ctx.dataset.label}: ${naira.format(ctx.parsed.y)}`;
                }
                const point = (latest.points || [])[ctx.dataIndex];
                if (!point) return "";
                const kind = point.pending ? "live" : "history";
                const decision = point.decision || kind;
                return `${naira.format(point.amount)} · ${decision} · Z=${point.z}`;
              },
            },
          },
        },
        scales: {
          x: {
            title: { display: true, text: xTitle, color: "#8b9bb8" },
            ticks: { color: "#8b9bb8" },
            grid: { color: "rgba(36, 48, 73, 0.8)" },
          },
          y: {
            title: { display: true, text: "Amount (₦)", color: "#8b9bb8" },
            suggestedMax,
            ticks: {
              color: "#8b9bb8",
              callback: (value) => naira.format(value),
            },
            grid: { color: "rgba(36, 48, 73, 0.8)" },
            beginAtZero: true,
          },
        },
      },
    });
  }

  function applyPayload(payload) {
    const pointKey = lastPointKey(payload);
    const firstPaint = seenPointKey === null;
    const newPayment = !firstPaint && pointKey !== "" && pointKey !== seenPointKey;
    const bandsMoved =
      !firstPaint &&
      (payload.ewma_avg !== latest.ewma_avg || payload.ewma_std !== latest.ewma_std);

    latest = payload;
    seenPointKey = pointKey;
    paintBaseline(payload);

    if (newPayment) {
      startNotice();
    }

    if (!chart) {
      createChart(payload);
      return;
    }

    patchChart(payload, { animateBands: bandsMoved && !newPayment });
  }

  async function refresh() {
    const response = await fetch(
      `/admin/api/customers/${encodeURIComponent(customerId)}/control-chart?category=${encodeURIComponent(category)}`,
      { credentials: "same-origin" }
    );
    if (response.status === 401) {
      window.location.href = "/admin";
      return;
    }
    if (response.status === 404) {
      showStatus(`No customer named ${customerId}. Seed or score them first.`);
      return;
    }
    if (response.status === 400) {
      showStatus("Unknown payment category.");
      return;
    }
    if (!response.ok) {
      showStatus("Could not load the control chart.");
      return;
    }
    const payload = await response.json();
    paintLatency(payload.latency_ms);

    if (!payload.points.length && payload.ewma_avg === 0 && payload.ewma_std === 0) {
      showStatus(`No settled ${payload.category} yet for this customer.`);
    } else {
      showStatus("");
    }

    const next = fingerprint(payload);
    if (next === lastFingerprint) {
      return;
    }
    lastFingerprint = next;
    applyPayload(payload);
  }

  if (categorySelect) {
    categorySelect.addEventListener("change", () => {
      category = categorySelect.value;
      lastFingerprint = "";
      seenPointKey = null;
      stopNotice();
      const url = new URL(window.location.href);
      url.searchParams.set("category", category);
      history.replaceState(null, "", url);
      refresh();
    });
  }

  refresh();
  setInterval(refresh, 2000);
})();
