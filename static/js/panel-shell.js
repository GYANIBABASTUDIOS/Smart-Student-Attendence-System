(() => {
    const toggle = document.getElementById("panel-menu-toggle");
    const sidebar = document.getElementById("panel-sidebar");
    const scrim = document.getElementById("panel-sidebar-scrim");

    const closeSidebar = () => {
        if (!sidebar || !toggle || !scrim) return;
        sidebar.classList.remove("open");
        toggle.setAttribute("aria-expanded", "false");
        toggle.setAttribute("aria-label", "Open navigation");
        scrim.hidden = true;
    };

    if (toggle && sidebar && scrim) {
        toggle.addEventListener("click", () => {
            const open = sidebar.classList.toggle("open");
            toggle.setAttribute("aria-expanded", String(open));
            toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
            scrim.hidden = !open;
        });
        scrim.addEventListener("click", closeSidebar);
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") closeSidebar();
        });
    }

    const dataElement = document.getElementById("weekly-attendance-data");
    const canvas = document.getElementById("weekly-attendance-chart");
    if (!dataElement || !canvas || typeof Chart === "undefined") return;

    let trend;
    try {
        trend = JSON.parse(dataElement.textContent || "[]");
    } catch (error) {
        console.error("Could not read weekly attendance data", error);
        return;
    }

    new Chart(canvas, {
        type: "bar",
        data: {
            labels: trend.map((day) => day.label),
            datasets: [
                {
                    label: "Present (incl. late)",
                    data: trend.map((day) => Number(day.present || 0) + Number(day.late || 0)),
                    backgroundColor: "#4b78dd",
                    borderRadius: 4,
                    maxBarThickness: 30,
                },
                {
                    label: "Absent",
                    data: trend.map((day) => Number(day.absent || 0)),
                    backgroundColor: "#e5a64a",
                    borderRadius: 4,
                    maxBarThickness: 30,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: {
                    position: "bottom",
                    labels: { usePointStyle: true, pointStyle: "circle", boxWidth: 7, boxHeight: 7, padding: 18, color: "#758198", font: { family: "Inter", size: 10 } },
                },
                tooltip: { backgroundColor: "#17243b", padding: 10, titleFont: { family: "Inter" }, bodyFont: { family: "Inter" } },
            },
            scales: {
                x: { grid: { display: false }, border: { display: false }, ticks: { color: "#8994a7", font: { family: "Inter", size: 9 } } },
                y: { beginAtZero: true, border: { display: false, dash: [3, 3] }, grid: { color: "#edf0f5" }, ticks: { precision: 0, color: "#8994a7", font: { family: "Inter", size: 9 } } },
            },
        },
    });
})();
