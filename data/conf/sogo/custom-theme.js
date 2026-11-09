(function () {
  'use strict';

  angular.module('SOGo.Common').config(['$mdThemingProvider', function ($mdThemingProvider) {
    var navy = $mdThemingProvider.extendPalette('indigo', {
      '500': '0B3970', '600': '092F63', '700': '09265B', '800': '071E49'
    });
    var teal = $mdThemingProvider.extendPalette('cyan', {
      '300': '78E5DB', '400': '38D6CA', '500': '00BDB4', '600': '009A93', '700': '007E79'
    });
    $mdThemingProvider.definePalette('wismail-navy', navy);
    $mdThemingProvider.definePalette('wismail-teal', teal);
    $mdThemingProvider.theme('default')
      .primaryPalette('wismail-navy')
      .accentPalette('wismail-teal');
  }]);

  document.addEventListener('DOMContentLoaded', function () {
    var style = document.createElement('style');
    style.textContent = [
      'body { font-family: "Noto Sans", "Segoe UI", sans-serif; }',
      'md-toolbar, .md-toolbar-tools { background-color: #09265b !important; color: #f4fbfc !important; }',
      'md-toolbar .md-button, md-toolbar md-icon { color: #f4fbfc !important; }',
      'md-sidenav { border-right: 1px solid #d9e7ec; }',
      '.md-button.md-primary.md-raised, .md-button.md-primary.md-fab { background-color: #0b3970 !important; color: white !important; }',
      '.md-button.md-accent.md-raised, .md-button.md-accent.md-fab { background-color: #007e79 !important; color: white !important; }',
      'md-input-container.md-input-focused .md-input { border-color: #00bdb4 !important; }',
      'md-input-container.md-input-focused label { color: #007e79 !important; }',
      '.md-button:focus-visible, a:focus-visible { outline: 2px solid #00bdb4; outline-offset: 2px; }'
    ].join('\n');
    document.head.appendChild(style);
  });
})();
