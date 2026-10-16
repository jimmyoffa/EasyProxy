/* Shared top navigation for every EasyProxy page.
   Injected by static/ep-nav.js so all templates stay in sync.
   Desktop: centered link bar. Mobile (<=900px): floating hamburger button (top-left) + off-canvas sidebar. */
(function () {
    const groups = [
        [
            {label: 'Home', href: '/', paths: ['/']},
            {label: 'Playlist Builder', href: '/builder', paths: ['/builder', '/playlist/builder']},
            {label: 'URL Generator', href: '/url-generator'},
            {label: 'DVR Recordings', href: '/recordings'},
        ],
        [
            {label: 'Admin', href: '/admin'},
            {label: 'TorProxy', href: '/admin/torproxy'},
            {label: 'NordVPN', href: '/admin/nordvpn'},
            {label: 'Custom WireGuard', href: '/admin/wireguard'},
        ],
        [
            {label: 'API Docs', href: '/docs'},
            {label: 'ReDoc', href: '/redoc'},
            {label: 'Info', href: '/info'},
        ],
    ];

    const params = new URLSearchParams(window.location.search);
    const password = params.get('api_password');
    const suffix = password ? '?api_password=' + encodeURIComponent(password) : '';
    const path = window.location.pathname.replace(/\/+$/, '') || '/';

    const font = "font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;";
    const ease = 'cubic-bezier(.22,1,.36,1)';

    const style = document.createElement('style');
    style.textContent = [
        '.ep-nav{position:fixed;top:0;left:0;right:0;z-index:100;',
        'background:rgba(9,9,15,.82);backdrop-filter:blur(20px);',
        'border-bottom:1px solid rgba(255,255,255,.08)}',
        '.ep-nav-inner{max-width:1100px;margin:0 auto;display:flex;gap:6px;',
        'align-items:center;justify-content:center;',
        'padding:9px 1.5rem;overflow-x:auto;scrollbar-width:none}',
        '.ep-nav-inner::-webkit-scrollbar{display:none}',
        '.ep-nav-link{flex:none;padding:7px 12px;border-radius:50px;text-decoration:none;',
        font,
        'font-size:.82rem;font-weight:500;color:#a1a1aa;border:1px solid transparent;',
        'transition:color .2s,background .2s,border-color .2s;white-space:nowrap}',
        '.ep-nav-link:hover{color:#fff;background:rgba(255,255,255,.06)}',
        '.ep-nav-link:focus-visible{outline:2px solid #6366f1;outline-offset:2px}',
        '.ep-nav-link.active{color:#fff;background:rgba(99,102,241,.18);border-color:rgba(99,102,241,.5)}',
        '.ep-nav-sep{flex:none;width:1px;height:18px;background:rgba(255,255,255,.12);margin:0 4px}',
        'body.ep-nav-padded{padding-top:56px}',

        '.ep-nav-menu{display:none;align-items:center;justify-content:center;',
        'width:44px;height:44px;margin-right:auto;padding:0;border:1px solid rgba(255,255,255,.1);border-radius:12px;',
        'background:rgba(13,13,20,.92);color:#e4e4e7;cursor:pointer;',
        'transition:color .2s,background .2s}',
        '.ep-nav-menu:hover{color:#fff;background:rgba(39,39,46,.95)}',
        '.ep-nav-menu:focus-visible{outline:2px solid #6366f1;outline-offset:2px}',

        '.ep-nav-overlay{position:fixed;inset:0;z-index:105;background:rgba(0,0,0,.55);',
        'opacity:0;pointer-events:none;transition:opacity .35s ' + ease + '}',
        '.ep-nav-overlay.is-open{opacity:1;pointer-events:auto}',

        '.ep-nav-drawer{position:fixed;top:0;left:0;bottom:0;z-index:110;',
        'width:min(300px,85vw);display:flex;flex-direction:column;gap:4px;',
        'padding:16px;overflow-y:auto;background:rgba(13,13,20,.98);',
        'backdrop-filter:blur(20px);border-right:1px solid rgba(255,255,255,.08);',
        'transform:translateX(-100%);opacity:0;filter:blur(2px);',
        'visibility:hidden;pointer-events:none;',
        'transition:transform .35s ' + ease + ',opacity .35s ' + ease + ',',
        'filter .35s ' + ease + ',visibility 0s linear .35s}',
        '.ep-nav-drawer[data-open="true"]{transform:translateX(0);opacity:1;filter:blur(0);',
        'visibility:visible;pointer-events:auto;',
        'transition:transform .4s ' + ease + ',opacity .4s ' + ease + ',',
        'filter .4s ' + ease + ',visibility 0s}',
        '.ep-nav-drawer-close{align-self:flex-end;display:flex;align-items:center;',
        'justify-content:center;width:44px;height:44px;padding:0;border:0;border-radius:12px;',
        'background:transparent;color:#a1a1aa;cursor:pointer;',
        'transition:color .2s,background .2s}',
        '.ep-nav-drawer-close:hover{color:#fff;background:rgba(255,255,255,.06)}',
        '.ep-nav-drawer-close:focus-visible{outline:2px solid #6366f1;outline-offset:2px}',
        '.ep-nav-drawer-link{padding:12px 14px;border-radius:12px;text-decoration:none;',
        font,
        'font-size:.95rem;font-weight:500;color:#d4d4d8;',
        'transition:color .2s,background .2s}',
        '.ep-nav-drawer-link:hover{color:#fff;background:rgba(255,255,255,.06)}',
        '.ep-nav-drawer-link:focus-visible{outline:2px solid #6366f1;outline-offset:2px}',
        '.ep-nav-drawer-link.active{color:#fff;background:rgba(99,102,241,.18)}',
        '.ep-nav-drawer-sep{height:1px;background:rgba(255,255,255,.12);margin:8px 6px}',
        'body.ep-nav-locked{overflow:hidden}',

        '@media(max-width:900px){.ep-nav{background:transparent;backdrop-filter:none;border-bottom:0;pointer-events:none}',
        '.ep-nav-inner{justify-content:flex-start;padding:4px 1rem}',
        '.ep-nav-inner .ep-nav-link,.ep-nav-inner .ep-nav-sep{display:none}',
        '.ep-nav-menu{display:flex;pointer-events:auto}',
        'body.ep-nav-padded{padding-top:0}}',
        '@media(prefers-reduced-motion:reduce){.ep-nav-drawer,.ep-nav-overlay{transition:none}}',
    ].join('');

    const makeLink = (item, cls) => {
        const active = (item.paths || [item.href]).includes(path);
        const link = document.createElement('a');
        link.className = cls + (active ? ' active' : '');
        link.href = item.href + suffix;
        link.textContent = item.label;
        if (active) link.setAttribute('aria-current', 'page');
        return link;
    };

    const nav = document.createElement('nav');
    nav.className = 'ep-nav';
    nav.setAttribute('aria-label', 'Main navigation');
    const inner = document.createElement('div');
    inner.className = 'ep-nav-inner';

    groups.forEach((group, index) => {
        if (index) {
            const separator = document.createElement('span');
            separator.className = 'ep-nav-sep';
            inner.appendChild(separator);
        }
        group.forEach(item => inner.appendChild(makeLink(item, 'ep-nav-link')));
    });

    const menuBtn = document.createElement('button');
    menuBtn.type = 'button';
    menuBtn.className = 'ep-nav-menu';
    menuBtn.setAttribute('aria-label', 'Open navigation menu');
    menuBtn.setAttribute('aria-expanded', 'false');
    menuBtn.setAttribute('aria-controls', 'ep-nav-drawer');
    menuBtn.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>';
    inner.appendChild(menuBtn);
    nav.appendChild(inner);

    const overlay = document.createElement('div');
    overlay.className = 'ep-nav-overlay';

    const drawer = document.createElement('nav');
    drawer.className = 'ep-nav-drawer';
    drawer.id = 'ep-nav-drawer';
    drawer.setAttribute('aria-label', 'Navigation menu');
    drawer.setAttribute('aria-hidden', 'true');
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'ep-nav-drawer-close';
    closeBtn.setAttribute('aria-label', 'Close navigation menu');
    closeBtn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    drawer.appendChild(closeBtn);
    groups.forEach((group, index) => {
        if (index) {
            const separator = document.createElement('div');
            separator.className = 'ep-nav-drawer-sep';
            drawer.appendChild(separator);
        }
        group.forEach(item => drawer.appendChild(makeLink(item, 'ep-nav-drawer-link')));
    });

    document.head.appendChild(style);
    document.body.insertBefore(nav, document.body.firstChild);
    document.body.appendChild(overlay);
    document.body.appendChild(drawer);
    document.body.classList.add('ep-nav-padded');

    let open = false;
    const setOpen = value => {
        open = value;
        drawer.dataset.open = String(value);
        overlay.classList.toggle('is-open', value);
        document.body.classList.toggle('ep-nav-locked', value);
        menuBtn.setAttribute('aria-expanded', String(value));
        drawer.setAttribute('aria-hidden', String(!value));
        if (value) closeBtn.focus();
    };
    const close = focusBack => {
        if (!open) return;
        setOpen(false);
        if (focusBack) menuBtn.focus();
    };

    menuBtn.addEventListener('click', () => setOpen(true));
    closeBtn.addEventListener('click', () => close(true));
    overlay.addEventListener('click', () => close(true));
    drawer.addEventListener('click', event => {
        if (event.target.closest('a')) close(false);
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') close(true);
        if (event.key !== 'Tab' || !open) return;
        const focusable = drawer.querySelectorAll('a,button');
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });
    window.addEventListener('resize', () => {
        if (!window.matchMedia('(max-width: 900px)').matches) close(false);
    });
})();
