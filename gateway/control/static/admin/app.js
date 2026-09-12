// App entry: wires core + pages. Kept as /admin-ui/app.js entry (module).
import { installDelegates, render, applyCompact, UIS, $ } from './core.js';
import './page_dashboard.js';
import './page_providers.js';
import './page_models.js';
import './page_policy.js';
import './page_monitoring.js';

window.__render = render;

installDelegates();
applyCompact();
window.addEventListener('hashchange', ()=>render());
document.documentElement.lang = UIS.lang;
render();
