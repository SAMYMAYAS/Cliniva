"""Zoom natif WebView2 : même mise en page que le zoom du navigateur."""
import math

LEVELS = (25, 33, 50, 67, 75, 80, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400, 500)


def zoom_level(current, action, value=None):
    if action == 'status':
        return current
    if action == 'reset':
        return 100
    if action == 'in':
        return next((level for level in LEVELS if level > current + .5), max(current, LEVELS[-1]))
    if action == 'out':
        return next((level for level in reversed(LEVELS) if level < current - .5), min(current, LEVELS[0]))
    if action == 'set' and not isinstance(value, bool):
        level = float(value)
        if math.isfinite(level):
            return max(LEVELS[0], min(LEVELS[-1], level))
    raise ValueError('Commande de zoom invalide.')


def update_native_zoom(native, action='status', value=None):
    """À appeler sur le thread de la fenêtre native Windows."""
    controls = [native]
    while controls:
        control = controls.pop()
        if hasattr(control, 'ZoomFactor') and hasattr(control, 'CoreWebView2'):
            core = control.CoreWebView2
            if core is None:
                return {'ok': False, 'error': 'Le navigateur intégré est en cours de chargement.'}
            current = float(control.ZoomFactor) * 100
            target = zoom_level(current, action, value)
            core.Settings.IsZoomControlEnabled = True
            core.Settings.AreBrowserAcceleratorKeysEnabled = True
            if action != 'status':
                control.ZoomFactor = target / 100
            return {'ok': True, 'percent': round(float(control.ZoomFactor) * 100)}
        controls.extend(list(getattr(control, 'Controls', ())))
    return {'ok': False, 'error': 'Le zoom natif nécessite le navigateur Windows WebView2.'}
