/**
 * Dark Mode Toggle for Smart Attendance System
 * Handles theme switching with localStorage persistence
 */

(function() {
    'use strict';

    const THEME_KEY = 'smart-attendance-theme';
    const DARK = 'dark';
    const LIGHT = 'light';

    /**
     * Get the current theme from localStorage or system preference
     */
    function getStoredTheme() {
        const stored = localStorage.getItem(THEME_KEY);
        if (stored) {
            return stored;
        }
        
        // Check system preference
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
            return DARK;
        }
        
        return LIGHT;
    }

    /**
     * Set the theme on the document
     */
    function setTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem(THEME_KEY, theme);
        
        // Update toggle button icon
        updateToggleIcon(theme);
        
        // Update Chart.js if present
        updateChartColors(theme);
        
        // Dispatch custom event for other scripts
        window.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
    }

    /**
     * Toggle between light and dark themes
     */
    function toggleTheme() {
        const current = document.documentElement.getAttribute('data-theme') || LIGHT;
        const newTheme = current === DARK ? LIGHT : DARK;
        setTheme(newTheme);
    }

    /**
     * Update the toggle button icon
     */
    function updateToggleIcon(theme) {
        const toggleBtn = document.querySelector('.theme-toggle');
        if (!toggleBtn) return;

        const sunIcon = toggleBtn.querySelector('.fa-sun');
        const moonIcon = toggleBtn.querySelector('.fa-moon');

        if (theme === DARK) {
            if (sunIcon) sunIcon.style.display = 'inline-block';
            if (moonIcon) moonIcon.style.display = 'none';
            toggleBtn.setAttribute('aria-label', 'Switch to light mode');
            toggleBtn.setAttribute('title', 'Switch to light mode');
        } else {
            if (sunIcon) sunIcon.style.display = 'none';
            if (moonIcon) moonIcon.style.display = 'inline-block';
            toggleBtn.setAttribute('aria-label', 'Switch to dark mode');
            toggleBtn.setAttribute('title', 'Switch to dark mode');
        }
    }

    /**
     * Update Chart.js colors for dark mode
     */
    function updateChartColors(theme) {
        if (typeof Chart === 'undefined') return;

        const isDark = theme === DARK;
        const textColor = isDark ? '#e8e8e8' : '#1a1a2e';
        const gridColor = isDark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.1)';

        // Update Chart.js defaults
        Chart.defaults.color = textColor;
        Chart.defaults.borderColor = gridColor;

        // Update all existing charts
        Object.values(Chart.instances || {}).forEach(chart => {
            if (chart.options.scales) {
                Object.values(chart.options.scales).forEach(scale => {
                    if (scale.ticks) scale.ticks.color = textColor;
                    if (scale.grid) scale.grid.color = gridColor;
                });
            }
            if (chart.options.plugins && chart.options.plugins.legend) {
                chart.options.plugins.legend.labels = chart.options.plugins.legend.labels || {};
                chart.options.plugins.legend.labels.color = textColor;
            }
            chart.update('none');
        });
    }

    /**
     * Create and inject the theme toggle button
     */
    function createToggleButton() {
        // Check if button already exists
        if (document.querySelector('.theme-toggle')) return;

        const navbar = document.querySelector('.navbar-nav.me-auto');
        if (!navbar) return;

        // Create toggle button
        const toggleBtn = document.createElement('button');
        toggleBtn.className = 'theme-toggle';
        toggleBtn.type = 'button';
        toggleBtn.setAttribute('aria-label', 'Toggle dark mode');
        toggleBtn.setAttribute('title', 'Toggle dark mode');
        toggleBtn.innerHTML = `
            <i class="fas fa-moon" aria-hidden="true"></i>
            <i class="fas fa-sun" aria-hidden="true"></i>
        `;

        // Add click handler
        toggleBtn.addEventListener('click', toggleTheme);

        // Insert after navbar
        const navbarCollapse = document.querySelector('.navbar-collapse');
        if (navbarCollapse) {
            const container = document.createElement('div');
            container.className = 'd-flex align-items-center';
            container.appendChild(toggleBtn);
            navbarCollapse.appendChild(container);
        }
    }

    /**
     * Initialize dark mode
     */
    function init() {
        // Apply stored theme immediately (before DOM ready to prevent flash)
        const theme = getStoredTheme();
        document.documentElement.setAttribute('data-theme', theme);

        // Wait for DOM to be ready
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function() {
                createToggleButton();
                updateToggleIcon(theme);
            });
        } else {
            createToggleButton();
            updateToggleIcon(theme);
        }

        // Listen for system theme changes
        if (window.matchMedia) {
            window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e) {
                // Only auto-switch if user hasn't manually set a preference
                if (!localStorage.getItem(THEME_KEY)) {
                    setTheme(e.matches ? DARK : LIGHT);
                }
            });
        }
    }

    // Expose functions globally
    window.darkMode = {
        toggle: toggleTheme,
        set: setTheme,
        get: function() {
            return document.documentElement.getAttribute('data-theme') || LIGHT;
        },
        isDark: function() {
            return this.get() === DARK;
        }
    };

    // Initialize
    init();

})();
