/* Le navigateur normal conserve ses raccourcis ; Cliniva pilote le zoom WebView2. */
(() => {
    if (window.clinivaInterfaceZoom) return;
    const storageKey = 'clinivaInterfaceZoomPercent';
    let api = null;
    let queue = Promise.resolve();
    let resizeTimer;
    function display(result) {
        if (!result?.ok) return result;
        document.querySelectorAll('[data-interface-zoom-controls]').forEach(group => {
            group.hidden = false;
            group.style.display = 'inline-flex';
        });
        document.querySelectorAll('[data-interface-zoom-percent]').forEach(label => label.textContent = result.percent + ' %');
        try { localStorage.setItem(storageKey, String(result.percent)); } catch (_) {}
        return result;
    }
    function request(action, value) {
        queue = queue.catch(() => {}).then(async () => {
            if (!api) return;
            const result = await api.interface_zoom(action, value ?? null);
            if (!result?.ok) {
                console.warn('Zoom de l’interface indisponible :', result?.error || 'navigateur non prêt');
                return result;
            }
            return display(result);
        }).catch(error => {
            console.warn('Zoom de l’interface indisponible :', error.message);
        });
        return queue;
    }
    function connect() {
        const bridge = window.pywebview?.api;
        if (api || typeof bridge?.interface_zoom !== 'function') return;
        api = bridge;
        let saved;
        try { saved = Number(localStorage.getItem(storageKey)); } catch (_) {}
        request(Number.isFinite(saved) && saved >= 25 && saved <= 500 ? 'set' : 'status', saved);
    }
    window.clinivaInterfaceZoom = {request};
    document.addEventListener('click', event => {
        const button = event.target.closest('[data-interface-zoom-action]');
        if (button && api) request(button.dataset.interfaceZoomAction);
    });
    // Un raccourci natif ou Ctrl+molette change le viewport ; synchroniser le pourcentage.
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => { if (api) request('status'); }, 100);
    });
    window.addEventListener('pywebviewready', connect);
    connect();
})();
