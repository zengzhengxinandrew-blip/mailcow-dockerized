// redirect to mailcow login form
document.addEventListener('DOMContentLoaded', function () {
    var loginForm = document.forms.namedItem("loginForm");
    if (loginForm) {
        window.location.href = '/user';
    }
});
// logout function
function mc_logout() {
    fetch("/", {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded"
        },
        body: "logout=1"
    }).then(() => window.location.href = '/');
}

// Custom SOGo JS

// Change the visible font-size in the editor, this does not change the font of a html message by default
CKEDITOR.addCss("body {font-size: 16px !important}");

// Enable scayt by default
//CKEDITOR.config.scayt_autoStartup = true;

// Keep the bulk sender one click away from the SOGo mail toolbar.
(function () {
    'use strict';

    function updateToolbar(toolbar) {
        var settings = toolbar.querySelector('[ng-href="/user"], a[href="/user"]');

        if (settings) {
            var tooltip = settings.querySelector('md-tooltip');
            if (tooltip && /mailcow/i.test(tooltip.textContent)) {
                tooltip.textContent = tooltip.textContent.replace(/mailcow/gi, 'Wismail');
            }
        }

        if (toolbar.querySelector('.wismail-bulk-link')) return;

        var link = document.createElement('a');
        link.className = 'wismail-bulk-link';
        link.href = '/bulk/';
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.title = '批量发送';
        link.setAttribute('aria-label', '批量发送');

        var icon = document.createElement('span');
        icon.className = 'material-icons';
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = 'send';
        link.appendChild(icon);

        var label = document.createElement('span');
        label.className = 'wismail-bulk-label';
        label.textContent = '批量发送';
        link.appendChild(label);

        if (settings) {
            toolbar.insertBefore(link, settings);
        } else {
            var divider = toolbar.querySelector('.hide-xs');
            toolbar.insertBefore(link, divider || null);
        }
    }

    function updateToolbars() {
        var toolbars = document.querySelectorAll('.toolbar-main .sg-toolbar-group-last');
        for (var i = 0; i < toolbars.length; i++) updateToolbar(toolbars[i]);
    }

    document.addEventListener('DOMContentLoaded', function () {
        var scheduled = false;
        updateToolbars();
        new MutationObserver(function () {
            if (scheduled) return;
            scheduled = true;
            window.requestAnimationFrame(function () {
                scheduled = false;
                updateToolbars();
            });
        }).observe(document.body, { childList: true, subtree: true });
    });
})();

