/**
 * Server Logs: Main Application
 * Phase 4: Polish - States, Keyboard Shortcuts, Animations, Responsive, Performance
 * Updated: Using xterm.js for log display (like runs and executions)
 */

// xterm.js helpers for Server Logs
let __serverLogsXtermReady = null;
let __serverLogsTerminal = null;
let __serverLogsTerminalMeta = null;

function getXtermTerminalCtor() {
  if (window.Terminal && typeof window.Terminal === 'function') return window.Terminal;
  if (window.xterm && window.xterm.Terminal) return window.xterm.Terminal;
  return null;
}

function getFitAddonCtor() {
  if (window.FitAddon && typeof window.FitAddon.FitAddon === 'function') return window.FitAddon.FitAddon;
  if (typeof window.FitAddon === 'function') return window.FitAddon;
  return null;
}

async function ensureServerLogsXtermReady() {
  if (getXtermTerminalCtor()) return;
  if (__serverLogsXtermReady) return __serverLogsXtermReady;

  const loadScript = (src) => new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error(`Failed to load script: ${src}`));
    document.head.appendChild(s);
  });

  const ensureCss = () => {
    const href = 'vendor/xterm/xterm.css';
    const already = Array.from(document.querySelectorAll('link[rel="stylesheet"]')).some(l => {
      const h = l.getAttribute('href') || l.href || '';
      return h.includes('vendor/xterm/xterm.css') || h.includes('/xterm@');
    });
    if (already) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = href;
    document.head.appendChild(link);
  };

  __serverLogsXtermReady = (async () => {
    ensureCss();

    const prevModule = window.module;
    const prevExports = window.exports;
    try {
      window.module = undefined;
      window.exports = undefined;
    } catch (_) {}

    const attempts = [
      { xterm: 'vendor/xterm/xterm.js', fit: 'vendor/xterm/xterm-addon-fit.js' },
      { xterm: 'https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js', fit: 'https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js' },
      { xterm: 'https://unpkg.com/xterm@5.3.0/lib/xterm.js', fit: 'https://unpkg.com/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js' }
    ];

    let lastErr = null;
    for (const a of attempts) {
      try {
        if (!getXtermTerminalCtor()) await loadScript(a.xterm);
        if (!window.FitAddon) await loadScript(a.fit);
        getXtermTerminalCtor();
        if (getXtermTerminalCtor()) break;
      } catch (e) {
        lastErr = e;
      }
    }

    try {
      window.module = prevModule;
      window.exports = prevExports;
    } catch (_) {}

    if (!getXtermTerminalCtor()) {
      console.error('[ServerLogs/xterm] Failed to load xterm.js', lastErr);
      throw lastErr || new Error('xterm.js failed to load');
    }
  })();

  return __serverLogsXtermReady;
}

function ensureServerLogsTerminal(containerEl) {
  if (!containerEl) return null;
  const TerminalCtor = getXtermTerminalCtor();
  if (!TerminalCtor) {
    console.error('[ServerLogs/xterm] xterm.js not loaded');
    return null;
  }

  if (!__serverLogsTerminalMeta) {
    __serverLogsTerminalMeta = {
      fitAddon: null,
      resizeObserver: null,
      follow: true,
      containerEl: null,
      wrapperEl: null
    };
  }

  __serverLogsTerminalMeta.containerEl = containerEl;
  __serverLogsTerminalMeta.wrapperEl = containerEl.parentElement;

  if (!__serverLogsTerminal) {
    __serverLogsTerminal = new TerminalCtor({
      convertEol: true,
      disableStdin: true,
      scrollback: 10000,
      fontFamily: 'JetBrains Mono, Courier New, monospace',
      fontSize: 12,
      lineHeight: 1.6,
      theme: {
        background: '#0F172A',
        foreground: '#E5E7EB',
        cursor: '#93C5FD',
        selectionBackground: 'rgba(59,130,246,0.35)',
        black: '#0F172A',
        red: '#EF4444',
        green: '#22C55E',
        yellow: '#EAB308',
        blue: '#60A5FA',
        magenta: '#A78BFA',
        cyan: '#22D3EE',
        white: '#E5E7EB',
        brightBlack: '#334155',
        brightRed: '#F87171',
        brightGreen: '#4ADE80',
        brightYellow: '#FBBF24',
        brightBlue: '#93C5FD',
        brightMagenta: '#C4B5FD',
        brightCyan: '#67E8F9',
        brightWhite: '#F8FAFC'
      }
    });

    const FitAddonCtor = getFitAddonCtor();
    if (FitAddonCtor) {
      __serverLogsTerminalMeta.fitAddon = new FitAddonCtor();
      try {
        __serverLogsTerminal.loadAddon(__serverLogsTerminalMeta.fitAddon);
      } catch (e) {
        console.warn('[ServerLogs/xterm] Failed to load fit addon:', e);
        __serverLogsTerminalMeta.fitAddon = null;
      }
    }
    
    // Track scroll position for auto-scroll behavior
    __serverLogsTerminal.onScroll(() => {
      const buffer = __serverLogsTerminal?.buffer?.active;
      if (buffer) {
        const isAtBottom = (buffer.baseY - buffer.viewportY) <= 1;
        __serverLogsTerminalMeta.follow = isAtBottom;
      }
    });
  }

  try {
    if (__serverLogsTerminal.element) {
      if (__serverLogsTerminal.element.parentElement !== containerEl) {
        containerEl.innerHTML = '';
        containerEl.appendChild(__serverLogsTerminal.element);
      }
    } else {
      containerEl.innerHTML = '';
      __serverLogsTerminal.open(containerEl);
    }
  } catch (e) {
    console.error('[ServerLogs/xterm] Failed to mount terminal:', e);
    return __serverLogsTerminal;
  }

  if (!__serverLogsTerminalMeta.resizeObserver && typeof ResizeObserver !== 'undefined' && __serverLogsTerminalMeta.wrapperEl) {
    __serverLogsTerminalMeta.resizeObserver = new ResizeObserver(() => {
      requestAnimationFrame(() => fitServerLogsTerminal());
    });
    __serverLogsTerminalMeta.resizeObserver.observe(__serverLogsTerminalMeta.wrapperEl);
  }

  setTimeout(() => fitServerLogsTerminal(), 0);
  return __serverLogsTerminal;
}

function fitServerLogsTerminal() {
  if (!__serverLogsTerminal || !__serverLogsTerminalMeta?.fitAddon) return;
  try {
    __serverLogsTerminalMeta.fitAddon.fit();
  } catch (e) {
    console.warn('[ServerLogs/xterm] Failed to fit terminal:', e);
  }
}

function appendServerLog(data) {
  const text = (data === null || data === undefined) ? '' : String(data);
  if (!__serverLogsTerminal) return;
  
  const normalized = text.replace(/\r?\n/g, '\r\n');
  const shouldFollow = __serverLogsTerminalMeta?.follow !== false;
  
  __serverLogsTerminal.write(normalized, () => {
    if (shouldFollow) {
      __serverLogsTerminal.scrollToBottom();
    }
  });
}

function clearServerLog() {
  if (!__serverLogsTerminal) return;
  try {
    __serverLogsTerminal.reset();
  } catch (e) {
    console.warn('[ServerLogs/xterm] Failed to reset terminal:', e);
  }
}

function formatLogLineForXterm(log) {
  const level = (log.level || 'INFO').toUpperCase();
  const timestamp = log.timestamp || '';
  const time = timestamp.match(/(\d{2}:\d{2}:\d{2})/) ? timestamp.match(/(\d{2}:\d{2}:\d{2})/)[1] : timestamp.split(' ')[1] || '--:--:--';
  const service = log.service || 'unknown';
  const message = log.message || '';
  
  // ANSI color codes based on level
  let levelColor = '\x1b[36m'; // cyan for INFO
  if (level.includes('ERROR')) levelColor = '\x1b[31m'; // red
  else if (level.includes('WARN')) levelColor = '\x1b[33m'; // yellow
  else if (level.includes('DEBUG')) levelColor = '\x1b[90m'; // gray
  
  const serviceColor = '\x1b[35m'; // magenta
  const reset = '\x1b[0m';
  
  // Format: [timestamp] LEVEL service: message
  return `${time} ${levelColor}${level.padEnd(5)}${reset} ${serviceColor}${service.padEnd(8)}${reset} ${message}\r\n`;
}

class ServerLogsApp {
  constructor(containerId) {
    this.container = document.getElementById(containerId);
    this.state = {
      logs: [],
      allLogs: [], // Все логи для виртуального скроллинга
      lastRenderedLogCount: 0, // Для инкрементального обновления xterm
      stats: {
        error: 0,
        warning: 0,
        info: 0,
        debug: 0
      },
      filters: {
        services: ['worker', 'backend', 'frontend'],
        levels: ['error', 'warning', 'info', 'debug'],
        search: '',
        searchRegex: false,
        timeRange: 'all'
      },
      viewMode: 'stream', // 'stream' | 'table'
      sidebarCollapsed: false,
      detailsCollapsed: true,
      selectedLog: null,
      loading: false,
      error: null,
      // Phase 2: New state
      searchMatches: {
        total: 0,
        current: 0,
        indices: []
      },
      tableSort: {
        column: null,
        direction: null // 'asc' | 'desc'
      },
      virtualScroll: {
        startIndex: 0,
        endIndex: 100,
        itemHeight: 40,
        containerHeight: 0
      },
      // Phase 3: New state
      pinnedLogs: new Set(this.loadPinnedLogs()), // Set of log IDs
      expandedLogs: new Set(), // Set of log IDs that are expanded
      errorIndices: [], // Indices of error logs for jump navigation
      // Phase 4: New state
      currentLogIndex: -1, // For keyboard navigation
      loadingProgress: 0, // For loading progress indicator
      keyboardShortcutsEnabled: true,
      lastRenderedLogCount: 0 // For incremental xterm updates
    };
    
    this.apiUrl = document.querySelector('meta[name="api-url"]')?.content || 'http://localhost:5000';
    this.debounceTimers = {}; // For debouncing
    this.memoizedResults = new Map(); // For memoization
    this._lastFilters = null; // Track filter changes for incremental updates
    this.init();
  }

  loadPinnedLogs() {
    try {
      const stored = localStorage.getItem('serverLogs_pinned');
      return stored ? JSON.parse(stored) : [];
    } catch (e) {
      return [];
    }
  }

  savePinnedLogs() {
    try {
      localStorage.setItem('serverLogs_pinned', JSON.stringify(Array.from(this.state.pinnedLogs)));
    } catch (e) {
      console.error('Failed to save pinned logs:', e);
    }
  }

  async init() {
    // Phase 3: Restore pinned logs from localStorage
    const storedPinned = this.loadPinnedLogs();
    this.state.pinnedLogs = new Set(storedPinned);
    
    // Initialize xterm.js
    try {
      await ensureServerLogsXtermReady();
      const viewer = document.getElementById('sl-viewer');
      if (viewer) {
        ensureServerLogsTerminal(viewer);
      }
    } catch (e) {
      console.error('[ServerLogs] Failed to initialize xterm.js:', e);
    }
    
    this.render();
    this.loadLogs();
    
    // Phase 4: Setup keyboard shortcuts
    this.setupKeyboardShortcuts();
    
    // Auto-refresh every 5 seconds
    this.autoRefreshInterval = setInterval(() => {
      this.loadLogs();
    }, 5000);
  }

  // Phase 4: Setup keyboard shortcuts
  setupKeyboardShortcuts() {
    document.addEventListener('keydown', (e) => {
      if (!this.state.keyboardShortcutsEnabled) return;
      
      // Ignore if typing in input
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
        // Allow / to focus search
        if (e.key === '/' && e.target.id !== 'sl-search-input') {
          e.preventDefault();
          const searchInput = document.getElementById('sl-search-input');
          if (searchInput) {
            searchInput.focus();
          }
        }
        return;
      }

      switch(e.key) {
        case '/':
          e.preventDefault();
          const searchInput = document.getElementById('sl-search-input');
          if (searchInput) {
            searchInput.focus();
            searchInput.select();
          }
          break;
        case 'j':
        case 'ArrowDown':
          e.preventDefault();
          this.navigateLogs(1);
          break;
        case 'k':
        case 'ArrowUp':
          e.preventDefault();
          this.navigateLogs(-1);
          break;
        case 'Escape':
          e.preventDefault();
          this.handleEscape();
          break;
        case 'n':
          if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            this.navigateSearch(1);
          }
          break;
        case 'p':
          if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            this.navigateSearch(-1);
          }
          break;
        case 'e':
          if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            this.jumpToNextError();
          }
          break;
      }
    });
  }

  // Phase 4: Navigate logs with keyboard
  navigateLogs(direction) {
    if (this.state.logs.length === 0) return;

    let newIndex = this.state.currentLogIndex + direction;
    if (newIndex < 0) newIndex = this.state.logs.length - 1;
    if (newIndex >= this.state.logs.length) newIndex = 0;

    this.state.currentLogIndex = newIndex;
    const log = this.state.logs[newIndex];
    const logId = this.getLogId(log);
    
    // Find and scroll to log
    setTimeout(() => {
      const allLogElements = document.querySelectorAll('[data-log-id]');
      for (let el of allLogElements) {
        let elLogId = el.dataset.logId;
        if (elLogId) {
          elLogId = elLogId.replace(/&quot;/g, '"').replace(/&#39;/g, "'");
        }
        if (elLogId === logId) {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          // Highlight briefly
          el.style.transition = 'background-color 0.3s';
          el.style.backgroundColor = 'rgba(59, 130, 246, 0.2)';
          setTimeout(() => {
            el.style.backgroundColor = '';
          }, 1000);
          break;
        }
      }
    }, 50);
  }

  // Phase 4: Handle Escape key
  handleEscape() {
    // Close details panel
    if (!this.state.detailsCollapsed) {
      this.state.detailsCollapsed = true;
      this.state.selectedLog = null;
      this.render();
      return;
    }

    // Clear search
    if (this.state.filters.search) {
      this.state.filters.search = '';
      const searchInput = document.getElementById('sl-search-input');
      if (searchInput) {
        searchInput.value = '';
      }
      this.updateSearchMatches();
      this.render();
      return;
    }

    // Close export menu if open
    const exportMenu = document.querySelector('.sl-export-menu');
    if (exportMenu) {
      document.body.removeChild(exportMenu);
    }
  }

  render() {
    if (!this.container) return;
    
    this.container.innerHTML = `
      <div class="server-logs-container">
        <!-- Header -->
        <div class="server-logs-header" id="sl-header">
          <div class="server-logs-header-left">
            <span class="sl-text-sm sl-text-secondary">Levels:</span>
            <div id="sl-level-filters"></div>
            <div class="flex-1 max-w-md ml-4 sl-search-container">
              <i class="fas fa-search sl-search-icon"></i>
              <input 
                type="text" 
                id="sl-search-input" 
                placeholder="Search logs... (regex supported)"
                value="${this.state.filters.search}"
                class="sl-search-input pl-10 pr-24"
              />
              ${this.state.searchMatches.total > 0 ? `
                <div class="sl-search-matches">
                  <span class="sl-font-mono">${this.state.searchMatches.current} of ${this.state.searchMatches.total}</span>
                  <div class="sl-search-nav">
                    <button id="sl-search-prev" class="sl-search-nav-btn" ${this.state.searchMatches.current === 1 ? 'disabled' : ''} title="Previous">
                      <i class="fas fa-chevron-up"></i>
                    </button>
                    <button id="sl-search-next" class="sl-search-nav-btn" ${this.state.searchMatches.current === this.state.searchMatches.total ? 'disabled' : ''} title="Next">
                      <i class="fas fa-chevron-down"></i>
                    </button>
                  </div>
                </div>
              ` : ''}
              <button 
                id="sl-regex-toggle" 
                class="sl-regex-toggle ${this.state.filters.searchRegex ? 'active' : ''}"
                title="Toggle regex"
              >
                .*
              </button>
            </div>
          </div>
          <div class="server-logs-header-right">
            <button 
              id="sl-view-stream" 
              class="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${this.state.viewMode === 'stream' ? 'bg-[var(--sl-accent-blue)]/10 border border-[var(--sl-accent-blue)] text-[var(--sl-accent-blue)]' : 'bg-[var(--sl-bg-elevated)] border border-[var(--sl-border-default)] text-[var(--sl-text-secondary)] hover:border-[var(--sl-border-accent)]'}"
            >
              Stream
            </button>
            <button 
              id="sl-view-table" 
              class="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${this.state.viewMode === 'table' ? 'bg-[var(--sl-accent-blue)]/10 border border-[var(--sl-accent-blue)] text-[var(--sl-accent-blue)]' : 'bg-[var(--sl-bg-elevated)] border border-[var(--sl-border-default)] text-[var(--sl-text-secondary)] hover:border-[var(--sl-border-accent)]'}"
            >
              Table
            </button>
            ${this.state.stats.error > 0 ? `
              <button 
                id="sl-jump-error-btn" 
                class="px-3 py-1.5 bg-[var(--sl-bg-elevated)] border border-[var(--sl-border-default)] rounded-lg text-xs font-medium text-[var(--sl-text-secondary)] hover:border-[var(--sl-color-error)] hover:text-[var(--sl-color-error)] transition-colors"
                title="Jump to next error"
              >
                <i class="fas fa-exclamation-triangle mr-1.5"></i>Jump to Error
              </button>
            ` : ''}
            <button 
              id="sl-export-btn" 
              class="px-3 py-1.5 bg-[var(--sl-bg-elevated)] border border-[var(--sl-border-default)] rounded-lg text-xs font-medium text-[var(--sl-text-secondary)] hover:border-[var(--sl-border-accent)] transition-colors"
            >
              <i class="fas fa-download mr-1.5"></i>Export
            </button>
            <button 
              id="sl-keyboard-hint"
              class="px-3 py-1.5 bg-[var(--sl-bg-elevated)] border border-[var(--sl-border-default)] rounded-lg text-xs font-medium text-[var(--sl-text-secondary)] hover:border-[var(--sl-border-accent)] transition-colors"
              title="Keyboard shortcuts"
            >
              <i class="fas fa-keyboard mr-1.5"></i>
              <span class="hidden md:inline">Shortcuts</span>
            </button>
          </div>
        </div>

        <!-- Main Content -->
        <div class="flex flex-1 overflow-hidden">
          <!-- Sidebar -->
          <div class="server-logs-sidebar ${this.state.sidebarCollapsed ? 'collapsed' : ''}" id="sl-sidebar">
            <div class="server-logs-sidebar-header">
              <span class="server-logs-sidebar-title">Scope</span>
              <button 
                id="sl-sidebar-toggle" 
                class="p-1 hover:bg-[var(--sl-bg-hover)] rounded text-[var(--sl-text-secondary)]"
              >
                <i class="fas fa-chevron-${this.state.sidebarCollapsed ? 'right' : 'left'} text-xs"></i>
              </button>
            </div>
            ${!this.state.sidebarCollapsed ? `
              <div class="server-logs-sidebar-section">
                <div class="server-logs-sidebar-section-title">Statistics</div>
                <div id="sl-sidebar-stats"></div>
              </div>
              ${this.state.pinnedLogs.size > 0 ? `
                <div class="server-logs-sidebar-section">
                  <div class="flex items-center justify-between">
                    <span class="server-logs-sidebar-section-title">Pinned</span>
                    <span class="sl-text-xs font-medium" style="color: var(--sl-color-warn)">${this.state.pinnedLogs.size}</span>
                  </div>
                </div>
              ` : ''}
              <div class="server-logs-sidebar-section mt-auto">
                <div class="server-logs-sidebar-section-title">Time Range</div>
                <div id="sl-sidebar-time-range"></div>
              </div>
            ` : ''}
          </div>

          <!-- Main Viewer -->
          <div class="server-logs-main">
            <div class="server-logs-viewer" id="sl-viewer" style="flex: 1; overflow: hidden; padding: 0;">
              <!-- xterm.js will mount here -->
            </div>
          </div>
        </div>

        <!-- Details Panel -->
        <div class="server-logs-details ${this.state.detailsCollapsed ? 'collapsed' : ''}" id="sl-details">
          <div class="server-logs-details-header">
            <div class="flex items-center gap-2">
              <span id="sl-details-level" class="sl-text-xs"></span>
              <span id="sl-details-service" class="sl-text-xs sl-text-secondary"></span>
              <span id="sl-details-timestamp" class="sl-text-xs sl-text-tertiary"></span>
            </div>
            <button 
              id="sl-details-close" 
              class="p-1 hover:bg-[var(--sl-bg-hover)] rounded text-[var(--sl-text-secondary)]"
            >
              <i class="fas fa-times text-xs"></i>
            </button>
          </div>
          <div class="server-logs-details-tabs">
            <button class="server-logs-details-tab active" data-tab="raw">Raw</button>
            <button class="server-logs-details-tab" data-tab="json">JSON</button>
            <button class="server-logs-details-tab" data-tab="context">Context</button>
          </div>
          <div class="server-logs-details-content" id="sl-details-content"></div>
        </div>
      </div>
    `;

    // Render after DOM is ready
    setTimeout(() => {
      this.renderFilters();
      this.renderSidebar();
      this.renderViewer();
      this.attachEventListeners();
    }, 0);
  }

  renderFilters() {
    // Level filters with counts
    const levelContainer = document.getElementById('sl-level-filters');
    if (levelContainer) {
      const levels = [
        { key: 'error', label: 'ERROR', count: this.state.stats.error },
        { key: 'warning', label: 'WARN', count: this.state.stats.warning },
        { key: 'info', label: 'INFO', count: this.state.stats.info },
        { key: 'debug', label: 'DEBUG', count: this.state.stats.debug }
      ];
      levelContainer.innerHTML = levels.map(level => `
        <button 
          class="sl-filter-chip ${this.state.filters.levels.includes(level.key) ? 'active' : ''}"
          data-level="${level.key}"
        >
          ${level.label}
          ${level.count > 0 ? `<span class="sl-filter-chip-count">${level.count}</span>` : ''}
        </button>
      `).join('');
    }
  }

  renderSidebar() {
    if (this.state.sidebarCollapsed) return;

    // Statistics
    const statsContainer = document.getElementById('sl-sidebar-stats');
    if (statsContainer) {
      statsContainer.innerHTML = `
        <div class="flex items-center justify-between py-1">
          <span class="sl-text-xs sl-text-secondary">Errors</span>
          <span class="sl-text-xs font-medium" style="color: var(--sl-color-error)">${this.state.stats.error}</span>
        </div>
        <div class="flex items-center justify-between py-1">
          <span class="sl-text-xs sl-text-secondary">Warnings</span>
          <span class="sl-text-xs font-medium" style="color: var(--sl-color-warn)">${this.state.stats.warning}</span>
        </div>
        <div class="flex items-center justify-between py-1">
          <span class="sl-text-xs sl-text-secondary">Info</span>
          <span class="sl-text-xs font-medium" style="color: var(--sl-color-info)">${this.state.stats.info}</span>
        </div>
        <div class="flex items-center justify-between py-1">
          <span class="sl-text-xs sl-text-secondary">Debug</span>
          <span class="sl-text-xs font-medium" style="color: var(--sl-color-debug)">${this.state.stats.debug}</span>
        </div>
      `;
    }

    // Time range
    const timeRangeContainer = document.getElementById('sl-sidebar-time-range');
    if (timeRangeContainer) {
      const ranges = ['1h', '24h', 'all'];
      timeRangeContainer.innerHTML = ranges.map(range => `
        <button 
          class="w-full text-left px-2 py-1.5 sl-text-xs sl-text-secondary hover:bg-[var(--sl-bg-hover)] rounded transition-colors"
          data-range="${range}"
        >
          Last ${range === 'all' ? 'All' : range}
        </button>
      `).join('');
    }
  }

  renderViewer() {
    const viewer = document.getElementById('sl-viewer');
    if (!viewer) return;

    // Ensure xterm terminal is initialized
    if (!__serverLogsTerminal) {
      ensureServerLogsTerminal(viewer);
    }

    if (this.state.loading) {
      if (__serverLogsTerminal) {
        clearServerLog();
        appendServerLog('Loading logs...\r\n');
      } else {
        viewer.innerHTML = this.renderLoading();
      }
      return;
    }

    if (this.state.error) {
      if (__serverLogsTerminal) {
        clearServerLog();
        appendServerLog(`Error: ${this.state.error}\r\n`);
      } else {
        viewer.innerHTML = this.renderError();
      }
      return;
    }

    if (this.state.logs.length === 0) {
      if (__serverLogsTerminal) {
        clearServerLog();
        appendServerLog('No logs available\r\n');
      } else {
        viewer.innerHTML = this.renderEmpty();
      }
      return;
    }

    // Render logs using xterm.js
    if (__serverLogsTerminal) {
      // Инкрементальное обновление: добавляем только новые логи
      const newLogsCount = this.state.logs.length;
      const lastCount = this.state.lastRenderedLogCount || 0;
      
      if (lastCount === 0 || newLogsCount < lastCount) {
        // Первая загрузка или логи были очищены - пересоздаем все
        clearServerLog();
        this.state.logs.forEach(log => {
          appendServerLog(formatLogLineForXterm(log));
        });
      } else if (newLogsCount > lastCount) {
        // Добавляем только новые логи
        const newLogs = this.state.logs.slice(lastCount);
        newLogs.forEach(log => {
          appendServerLog(formatLogLineForXterm(log));
        });
      }
      
      this.state.lastRenderedLogCount = newLogsCount;
      
      // Auto-scroll to bottom только если пользователь уже внизу
      const isAtBottom = __serverLogsTerminalMeta?.follow !== false;
      if (isAtBottom) {
        setTimeout(() => {
          if (__serverLogsTerminal) {
            __serverLogsTerminal.scrollToBottom();
          }
        }, 100);
      }
    } else {
      // Fallback to DOM rendering if xterm not available
      if (this.state.viewMode === 'stream') {
        viewer.innerHTML = this.renderStreamView();
      } else {
        viewer.innerHTML = this.renderTableView();
      }
    }
  }

  renderStreamView() {
    // Phase 4: Improved virtual scrolling with scroll listener
    const logsToRender = this.getVisibleLogs();
    
    // Setup scroll listener for virtual scrolling
    setTimeout(() => {
      const viewer = document.getElementById('sl-viewer');
      if (viewer && this.state.logs.length > 1000) {
        const handleScroll = this.debounce('scroll', () => {
          // Re-render visible logs on scroll
          this.renderViewer();
        }, 100);
        
        viewer.removeEventListener('scroll', this._scrollHandler);
        this._scrollHandler = handleScroll;
        viewer.addEventListener('scroll', handleScroll, { passive: true });
      }
    }, 100);
    
    return `
      <div class="flex flex-col" id="sl-stream-container">
        ${logsToRender.map((log, index) => {
          // Phase 4: Add fade-in animation for new logs
          const animationDelay = index < 20 ? index * 0.02 : 0;
          return `<div style="animation-delay: ${animationDelay}s" class="sl-fade-in">${this.renderLogLine(log)}</div>`;
        }).join('')}
      </div>
      ${this.state.logs.length > 1000 ? `
        <div class="sl-text-xs sl-text-tertiary text-center py-2">
          Showing ${logsToRender.length} of ${this.state.logs.length} logs
          <button 
            id="sl-load-more"
            class="ml-2 px-2 py-1 sl-text-xs border border-[var(--sl-border-default)] rounded hover:bg-[var(--sl-bg-hover)] sl-text-secondary"
          >
            Load More
          </button>
        </div>
      ` : ''}
    `;
  }

  getVisibleLogs() {
    // Phase 4: Improved virtual scrolling with viewport calculation
    const viewer = document.getElementById('sl-viewer');
    if (!viewer) {
      // Fallback: simple limit
      if (this.state.logs.length > 1000) {
        return this.state.logs.slice(0, 500);
      }
      return this.state.logs;
    }

    // Calculate visible range based on scroll position
    const itemHeight = 40; // Approximate height per log line
    const containerHeight = viewer.clientHeight;
    const scrollTop = viewer.scrollTop;
    
    const startIndex = Math.max(0, Math.floor(scrollTop / itemHeight) - 10); // Buffer
    const visibleCount = Math.ceil(containerHeight / itemHeight) + 20; // Buffer
    const endIndex = Math.min(this.state.logs.length, startIndex + visibleCount);

    // For very large datasets, limit initial render
    if (this.state.logs.length > 5000) {
      return this.state.logs.slice(startIndex, endIndex);
    }

    return this.state.logs;
  }

  renderTableView() {
    const sortColumn = this.state.tableSort.column;
    const sortDirection = this.state.tableSort.direction;
    
    return `
      <table class="w-full">
        <thead class="sticky top-0 bg-[var(--sl-bg-elevated)] border-b border-[var(--sl-border-muted)]">
          <tr>
            <th 
              class="px-2 py-1 text-left sl-text-sm font-medium sl-text-secondary uppercase sl-table-header-sortable ${sortColumn === 'timestamp' ? sortDirection : ''}"
              data-sort="timestamp"
            >
              Timestamp
            </th>
            <th 
              class="px-2 py-1 text-left sl-text-sm font-medium sl-text-secondary uppercase sl-table-header-sortable ${sortColumn === 'level' ? sortDirection : ''}"
              data-sort="level"
            >
              Level
            </th>
            <th 
              class="px-2 py-1 text-left sl-text-sm font-medium sl-text-secondary uppercase sl-table-header-sortable ${sortColumn === 'service' ? sortDirection : ''}"
              data-sort="service"
            >
              Service
            </th>
            <th 
              class="px-2 py-1 text-left sl-text-sm font-medium sl-text-secondary uppercase sl-table-header-sortable ${sortColumn === 'message' ? sortDirection : ''}"
              data-sort="message"
            >
              Message
            </th>
          </tr>
        </thead>
        <tbody>
          ${this.getSortedLogs().map(log => {
            const logId = this.getLogId(log);
            const message = this.highlightSearch(log.message || '');
            return `
            <tr 
              class="border-b border-[var(--sl-border-muted)] hover:bg-[var(--sl-bg-elevated)]/50 cursor-pointer transition-colors"
              data-log-id="${logId}"
            >
              <td class="px-2 py-1 sl-text-sm sl-text-tertiary sl-font-mono">${this.formatTime(log.timestamp)}</td>
              <td class="px-2 py-1">
                <span class="sl-level-badge sl-level-${(log.level || 'INFO').toLowerCase()}">${log.level || 'INFO'}</span>
              </td>
              <td class="px-2 py-1 sl-text-sm uppercase sl-font-mono" style="color: var(--sl-accent-purple)">${log.service || 'unknown'}</td>
              <td class="px-2 py-1 sl-text-base sl-text-primary sl-font-mono" style="word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">${message}</td>
            </tr>
          `;
          }).join('')}
        </tbody>
      </table>
    `;
  }

  getSortedLogs() {
    // Phase 4: Memoization for sorting
    const sortKey = `${this.state.tableSort.column}_${this.state.tableSort.direction}_${this.state.logs.length}`;
    
    return this.memoize(sortKey, () => {
      if (!this.state.tableSort.column || !this.state.tableSort.direction) {
        return this.state.logs;
      }

      const column = this.state.tableSort.column;
      const direction = this.state.tableSort.direction === 'asc' ? 1 : -1;
      const logs = [...this.state.logs];

      return logs.sort((a, b) => {
      let aVal, bVal;

      switch(column) {
        case 'timestamp':
          aVal = a.timestamp || '';
          bVal = b.timestamp || '';
          break;
        case 'level':
          aVal = (a.level || 'INFO').toUpperCase();
          bVal = (b.level || 'INFO').toUpperCase();
          break;
        case 'service':
          aVal = (a.service || '').toLowerCase();
          bVal = (b.service || '').toLowerCase();
          break;
        case 'message':
          aVal = (a.message || '').toLowerCase();
          bVal = (b.message || '').toLowerCase();
          break;
        default:
          return 0;
      }

      if (aVal < bVal) return -1 * direction;
      if (aVal > bVal) return 1 * direction;
      return 0;
    });
    });
  }

  renderLogLine(log) {
    const level = (log.level || 'INFO').toUpperCase();
    const logId = this.getLogId(log);
    const isSearchMatch = this.isSearchMatch(log);
    const isCurrentMatch = this.isCurrentSearchMatch(log);
    const isPinned = this.state.pinnedLogs.has(logId);
    const isExpanded = this.state.expandedLogs.has(logId);
    const message = this.highlightSearch(log.message || '');
    
    // Escape logId for use in HTML attributes
    const safeLogId = this.escapeHtml(logId).replace(/"/g, '&quot;');
    
    return `
      <div 
        class="flex flex-col ${isPinned ? 'bg-[rgba(245,158,11,0.05)]' : ''} ${isSearchMatch ? 'bg-[rgba(245,158,11,0.1)]' : ''} ${isCurrentMatch ? 'bg-[rgba(59,130,246,0.1)] border-l-2 border-l-[var(--sl-accent-blue)]' : ''}"
      >
        <div 
          class="flex items-start gap-3 px-2 py-1 border-b border-[var(--sl-border-muted)] hover:bg-[var(--sl-bg-elevated)]/50 transition-colors cursor-pointer group"
          data-log-id="${safeLogId}"
        >
          <span class="sl-text-sm sl-text-tertiary sl-font-mono shrink-0 w-24">${this.formatTime(log.timestamp)}</span>
          <span class="sl-level-badge sl-level-${level.toLowerCase()} shrink-0 w-20">${level}</span>
          <span class="sl-text-sm uppercase sl-font-mono shrink-0 w-20" style="color: var(--sl-accent-purple)">${log.service || 'unknown'}</span>
          <span class="flex-1 sl-text-base sl-text-primary sl-font-mono break-words" style="word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">${message}</span>
          <div class="flex items-center gap-2 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
            <button 
              class="p-1.5 hover:bg-[var(--sl-bg-hover)] rounded sl-text-secondary ${isPinned ? 'text-[var(--sl-color-warn)]' : ''}" 
              title="${isPinned ? 'Unpin log' : 'Pin log'}"
              data-action="pin"
              data-log-id="${safeLogId}"
            >
              <i class="fas fa-thumbtack sl-text-xs ${isPinned ? 'fa-rotate-90' : ''}"></i>
            </button>
            <button 
              class="p-1.5 hover:bg-[var(--sl-bg-hover)] rounded sl-text-secondary" 
              title="Copy raw"
              data-action="copy-raw"
              data-log-id="${safeLogId}"
            >
              <i class="fas fa-copy sl-text-xs"></i>
            </button>
            <button 
              class="p-1.5 hover:bg-[var(--sl-bg-hover)] rounded sl-text-secondary" 
              title="${isExpanded ? 'Collapse' : 'Expand'} details"
              data-action="expand"
              data-log-id="${safeLogId}"
            >
              <i class="fas fa-chevron-${isExpanded ? 'up' : 'down'} sl-text-xs"></i>
            </button>
          </div>
        </div>
        ${isExpanded ? `
          <div class="px-2 py-1 bg-[var(--sl-bg-elevated)] border-b border-[var(--sl-border-muted)]">
            <div class="flex gap-2 mb-2">
              <button 
                class="px-2 py-1 sl-text-xs border border-[var(--sl-border-default)] rounded hover:bg-[var(--sl-bg-hover)] sl-text-secondary"
                data-action="copy-raw"
                data-log-id="${safeLogId}"
              >
                <i class="fas fa-copy mr-1"></i>Copy Raw
              </button>
              <button 
                class="px-2 py-1 sl-text-xs border border-[var(--sl-border-default)] rounded hover:bg-[var(--sl-bg-hover)] sl-text-secondary"
                data-action="copy-json"
                data-log-id="${safeLogId}"
              >
                <i class="fas fa-copy mr-1"></i>Copy JSON
              </button>
              <button 
                class="px-2 py-1 sl-text-xs border border-[var(--sl-border-default)] rounded hover:bg-[var(--sl-bg-hover)] sl-text-secondary"
                data-action="copy-stack"
                data-log-id="${safeLogId}"
              >
                <i class="fas fa-copy mr-1"></i>Copy Stack
              </button>
            </div>
            <div class="space-y-2">
              <div>
                <div class="sl-text-xs sl-text-tertiary mb-1">Raw Log:</div>
                <pre class="sl-text-xs sl-text-secondary sl-font-mono whitespace-pre-wrap break-words bg-[var(--sl-bg-input)] p-2 rounded">${this.escapeHtml(log.raw || '')}</pre>
              </div>
              <div>
                <div class="sl-text-xs sl-text-tertiary mb-1">JSON:</div>
                <pre class="sl-text-xs sl-text-secondary sl-font-mono whitespace-pre-wrap break-words bg-[var(--sl-bg-input)] p-2 rounded">${this.escapeHtml(JSON.stringify(log, null, 2))}</pre>
              </div>
              ${this.extractStackTrace(log) ? `
                <div>
                  <div class="sl-text-xs sl-text-tertiary mb-1">Stack Trace:</div>
                  <pre class="sl-text-xs sl-text-secondary sl-font-mono whitespace-pre-wrap break-words bg-[var(--sl-bg-input)] p-2 rounded">${this.escapeHtml(this.extractStackTrace(log))}</pre>
                </div>
              ` : ''}
            </div>
          </div>
        ` : ''}
      </div>
    `;
  }

  extractStackTrace(log) {
    const text = log.raw || log.message || '';
    const stackMatch = text.match(/(?:Traceback|Stack|at\s+.*?:\d+|File\s+.*?,\s+line\s+\d+)/s);
    if (stackMatch) {
      return text.substring(text.indexOf(stackMatch[0]));
    }
    return null;
  }

  highlightSearch(text) {
    if (!this.state.filters.search) {
      return this.escapeHtml(text);
    }

    const search = this.state.filters.search;
    let regex;

    try {
      if (this.state.filters.searchRegex) {
        regex = new RegExp(`(${search})`, 'gi');
      } else {
        regex = new RegExp(`(${this.escapeRegex(search)})`, 'gi');
      }
    } catch (e) {
      // Invalid regex, fallback to simple search
      regex = new RegExp(`(${this.escapeRegex(search)})`, 'gi');
    }

    const escaped = this.escapeHtml(text);
    const parts = escaped.split(regex);
    
    // Find current match index in text
    const currentMatchIndex = this.state.searchMatches.current > 0 ? 
      this.state.searchMatches.indices[this.state.searchMatches.current - 1] : -1;
    let matchIndex = 0;
    
    return parts.map((part, i) => {
      // Check if this part matches the regex
      const testRegex = new RegExp(regex.source, regex.flags);
      if (testRegex.test(part)) {
        const isCurrent = currentMatchIndex >= 0 && 
          this.state.logs[currentMatchIndex] && 
          text === (this.state.logs[currentMatchIndex].message || '');
        matchIndex++;
        return `<mark class="${isCurrent ? 'sl-search-highlight-current' : 'sl-search-highlight'}">${part}</mark>`;
      }
      return part;
    }).join('');
  }

  escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  isSearchMatch(log) {
    if (!this.state.filters.search) return false;
    return this.state.searchMatches.indices.includes(this.state.logs.indexOf(log));
  }

  isCurrentSearchMatch(log) {
    if (!this.state.filters.search || this.state.searchMatches.current === 0) return false;
    const currentIndex = this.state.searchMatches.indices[this.state.searchMatches.current - 1];
    return this.state.logs.indexOf(log) === currentIndex;
  }

  renderLoading() {
    return `
      <div class="flex flex-col items-center justify-center h-full">
        <div class="relative w-16 h-16 mb-4">
          <div class="absolute inset-0 border-4 border-[var(--sl-border-muted)] rounded-full"></div>
          <div class="absolute inset-0 border-4 border-transparent border-t-[var(--sl-accent-blue)] rounded-full animate-spin"></div>
          <div class="absolute inset-0 flex items-center justify-center">
            <span class="sl-text-xs sl-text-tertiary font-mono">${Math.round(this.state.loadingProgress)}%</span>
          </div>
        </div>
        <p class="sl-text-sm sl-text-secondary mb-2">Loading logs...</p>
        <div class="w-64 h-1 bg-[var(--sl-bg-elevated)] rounded-full overflow-hidden">
          <div 
            class="h-full bg-[var(--sl-accent-blue)] transition-all duration-300 ease-out"
            style="width: ${this.state.loadingProgress}%"
          ></div>
        </div>
      </div>
    `;
  }

  renderError() {
    const errorMessage = this.state.error || 'Unknown error';
    const isNetworkError = errorMessage.includes('fetch') || errorMessage.includes('network') || errorMessage.includes('Failed to fetch');
    const isAuthError = errorMessage.includes('401') || errorMessage.includes('UNAUTHORIZED');
    
    return `
      <div class="flex flex-col items-center justify-center h-full px-4">
        <div class="text-center max-w-md">
          <div class="inline-flex items-center justify-center w-16 h-16 rounded-full bg-[rgba(220,38,38,0.1)] mb-4">
            <i class="fas fa-exclamation-triangle text-3xl" style="color: var(--sl-color-error)"></i>
          </div>
          <h3 class="sl-text-base font-semibold sl-text-primary mb-2">Error loading logs</h3>
          <p class="sl-text-sm sl-text-secondary mb-4">${this.escapeHtml(errorMessage)}</p>
          
          ${isNetworkError ? `
            <div class="bg-[rgba(59,130,246,0.1)] border border-[var(--sl-accent-blue)] rounded-lg p-3 mb-4 text-left">
              <p class="sl-text-xs sl-text-secondary mb-1"><strong>Network Error:</strong></p>
              <ul class="sl-text-xs sl-text-tertiary list-disc list-inside space-y-1">
                <li>Check your internet connection</li>
                <li>Verify API URL: ${this.apiUrl}</li>
                <li>Check if backend server is running</li>
              </ul>
            </div>
          ` : ''}
          
          ${isAuthError ? `
            <div class="bg-[rgba(245,158,11,0.1)] border border-[var(--sl-color-warn)] rounded-lg p-3 mb-4 text-left">
              <p class="sl-text-xs sl-text-secondary mb-1"><strong>Authentication Error:</strong></p>
              <ul class="sl-text-xs sl-text-tertiary list-disc list-inside space-y-1">
                <li>Check your authentication credentials</li>
                <li>Verify API access permissions</li>
                <li>Try refreshing the page</li>
              </ul>
            </div>
          ` : ''}
          
          <div class="flex gap-2 justify-center">
            <button 
              id="sl-error-retry"
              class="px-4 py-2 rounded-lg sl-text-sm font-medium transition-all duration-150 hover:scale-105 active:scale-95"
              style="background: var(--sl-accent-blue); color: white;"
            >
              <i class="fas fa-redo mr-2"></i>Retry
            </button>
            <button 
              id="sl-error-reload"
              class="px-4 py-2 rounded-lg sl-text-sm font-medium transition-all duration-150 hover:scale-105 active:scale-95 border border-[var(--sl-border-default)] sl-text-secondary hover:border-[var(--sl-border-accent)]"
            >
              <i class="fas fa-refresh mr-2"></i>Reload Page
            </button>
          </div>
        </div>
      </div>
    `;
  }

  renderEmpty() {
    const hasActiveFilters = 
      this.state.filters.services.length < 3 ||
      this.state.filters.levels.length < 4 ||
      this.state.filters.search;
    
    return `
      <div class="flex flex-col items-center justify-center h-full px-4">
        <div class="text-center max-w-sm">
          <div class="inline-flex items-center justify-center w-20 h-20 rounded-full bg-[rgba(100,116,139,0.1)] mb-4">
            <i class="fas fa-file-alt text-4xl opacity-50 sl-text-tertiary"></i>
          </div>
          <h3 class="sl-text-base font-semibold sl-text-primary mb-2">No logs found</h3>
          
          ${hasActiveFilters ? `
            <p class="sl-text-sm sl-text-secondary mb-4">Try adjusting your filters:</p>
            <div class="bg-[var(--sl-bg-elevated)] border border-[var(--sl-border-muted)] rounded-lg p-3 mb-4 text-left">
              <ul class="sl-text-xs sl-text-tertiary space-y-2">
                ${this.state.filters.services.length < 3 ? `
                  <li class="flex items-center gap-2">
                    <i class="fas fa-check-circle text-[var(--sl-color-warn)]"></i>
                    <span>Services filter is active (${this.state.filters.services.length} selected)</span>
                  </li>
                ` : ''}
                ${this.state.filters.levels.length < 4 ? `
                  <li class="flex items-center gap-2">
                    <i class="fas fa-check-circle text-[var(--sl-color-warn)]"></i>
                    <span>Levels filter is active (${this.state.filters.levels.length} selected)</span>
                  </li>
                ` : ''}
                ${this.state.filters.search ? `
                  <li class="flex items-center gap-2">
                    <i class="fas fa-check-circle text-[var(--sl-color-warn)]"></i>
                    <span>Search filter: "${this.escapeHtml(this.state.filters.search)}"</span>
                  </li>
                ` : ''}
              </ul>
            </div>
            <button 
              id="sl-empty-clear-filters"
              class="px-4 py-2 rounded-lg sl-text-sm font-medium transition-all duration-150 hover:scale-105 active:scale-95 border border-[var(--sl-border-default)] sl-text-secondary hover:border-[var(--sl-border-accent)]"
            >
              <i class="fas fa-filter mr-2"></i>Clear All Filters
            </button>
          ` : `
            <p class="sl-text-sm sl-text-secondary mb-4">Logs will appear here when available</p>
            <div class="bg-[var(--sl-bg-elevated)] border border-[var(--sl-border-muted)] rounded-lg p-3 text-left">
              <p class="sl-text-xs sl-text-tertiary mb-2"><strong>Tips:</strong></p>
              <ul class="sl-text-xs sl-text-tertiary list-disc list-inside space-y-1">
                <li>Check if services are running</li>
                <li>Verify log files exist</li>
                <li>Wait for new logs to appear</li>
              </ul>
            </div>
          `}
        </div>
      </div>
    `;
  }

  attachEventListeners() {
    // Use event delegation for dynamically created elements
    this.container.addEventListener('click', (e) => {
      // Level filters
      if (e.target.closest('[data-level]')) {
        const level = e.target.closest('[data-level]').dataset.level;
        this.toggleLevel(level);
        return;
      }

      // View mode
      if (e.target.id === 'sl-view-stream' || e.target.closest('#sl-view-stream')) {
        this.state.viewMode = 'stream';
        this.render();
        return;
      }

      if (e.target.id === 'sl-view-table' || e.target.closest('#sl-view-table')) {
        this.state.viewMode = 'table';
        this.render();
        return;
      }

      // Sidebar toggle
      if (e.target.id === 'sl-sidebar-toggle' || e.target.closest('#sl-sidebar-toggle')) {
        this.state.sidebarCollapsed = !this.state.sidebarCollapsed;
        this.render();
        return;
      }

      // Details close
      if (e.target.id === 'sl-details-close' || e.target.closest('#sl-details-close')) {
        this.state.detailsCollapsed = true;
        this.state.selectedLog = null;
        this.render();
        return;
      }

      // Log line click (but not on action buttons)
      const logLine = e.target.closest('[data-log-id]');
      if (logLine && !e.target.closest('[data-action]')) {
        let logId = logLine.dataset.logId;
        // Decode HTML entities
        if (logId) {
          logId = logId.replace(/&quot;/g, '"').replace(/&#39;/g, "'");
        }
        const log = this.state.logs.find(l => {
          const logIdStr = String(this.getLogId(l));
          return logIdStr === logId;
        });
        if (log) {
          this.selectLog(log);
        }
        return;
      }

      // Details tabs
      if (e.target.classList.contains('server-logs-details-tab')) {
        const tab = e.target.dataset.tab;
        this.switchDetailsTab(tab);
        return;
      }

      // Phase 3: Action buttons (pin, expand, copy)
      const actionButton = e.target.closest('[data-action]');
      if (actionButton) {
        e.stopPropagation(); // Prevent log line click
        const action = actionButton.dataset.action;
        let logId = actionButton.dataset.logId;
        // Decode HTML entities
        if (logId) {
          logId = logId.replace(/&quot;/g, '"').replace(/&#39;/g, "'");
        }
        const log = this.state.logs.find(l => this.getLogId(l) === logId);
        
        if (!log) return;

        switch(action) {
          case 'pin':
            this.togglePinLog(log);
            break;
          case 'expand':
            this.toggleExpandLog(log);
            break;
          case 'copy-raw':
            this.copyLog(log, 'raw');
            break;
          case 'copy-json':
            this.copyLog(log, 'json');
            break;
          case 'copy-stack':
            this.copyLog(log, 'stack');
            break;
        }
        return;
      }

      // Jump to error button
      if (e.target.id === 'sl-jump-error-btn' || e.target.closest('#sl-jump-error-btn')) {
        this.jumpToNextError();
        return;
      }

      // Export button
      if (e.target.id === 'sl-export-btn' || e.target.closest('#sl-export-btn')) {
        this.showExportMenu(e);
        return;
      }

      // Phase 4: Keyboard shortcuts hint
      if (e.target.id === 'sl-keyboard-hint' || e.target.closest('#sl-keyboard-hint')) {
        this.showKeyboardShortcuts();
        return;
      }

      // Phase 4: Error retry/reload
      if (e.target.id === 'sl-error-retry' || e.target.closest('#sl-error-retry')) {
        this.loadLogs();
        return;
      }

      if (e.target.id === 'sl-error-reload' || e.target.closest('#sl-error-reload')) {
        window.location.reload();
        return;
      }

      // Phase 4: Clear filters
      if (e.target.id === 'sl-empty-clear-filters' || e.target.closest('#sl-empty-clear-filters')) {
        this.state.filters.services = ['worker', 'backend', 'frontend'];
        this.state.filters.levels = ['error', 'warning', 'info', 'debug'];
        this.state.filters.search = '';
        const searchInput = document.getElementById('sl-search-input');
        if (searchInput) {
          searchInput.value = '';
        }
        this.loadLogs();
        return;
      }

      // Phase 4: Load more logs
      if (e.target.id === 'sl-load-more' || e.target.closest('#sl-load-more')) {
        this.loadMoreLogs();
        return;
      }
    });

    // Search input (needs direct listener for input event)
    const searchInput = document.getElementById('sl-search-input');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        // Phase 4: Use debounce utility
        this.debounce('search', () => {
          this.state.filters.search = e.target.value;
          this.updateSearchMatches();
          this.render();
        }, 300);
      });
    }

    // Regex toggle
    const regexToggle = document.getElementById('sl-regex-toggle');
    if (regexToggle) {
      regexToggle.addEventListener('click', () => {
        this.state.filters.searchRegex = !this.state.filters.searchRegex;
        this.updateSearchMatches();
        this.render();
      });
    }

    // Search navigation
    document.getElementById('sl-search-prev')?.addEventListener('click', () => {
      this.navigateSearch(-1);
    });
    document.getElementById('sl-search-next')?.addEventListener('click', () => {
      this.navigateSearch(1);
    });

    // Table sorting
    this.container.addEventListener('click', (e) => {
      const sortableHeader = e.target.closest('.sl-table-header-sortable');
      if (sortableHeader) {
        const column = sortableHeader.dataset.sort;
        this.handleTableSort(column);
        return;
      }
    });

    // Sidebar service checkboxes
    this.container.addEventListener('change', (e) => {
      if (e.target.type === 'checkbox' && e.target.dataset.service) {
        const service = e.target.dataset.service;
        if (e.target.checked) {
          if (!this.state.filters.services.includes(service)) {
            this.state.filters.services.push(service);
          }
        } else {
          this.state.filters.services = this.state.filters.services.filter(s => s !== service);
        }
        this.loadLogs();
      }
    });
  }

  getLogId(log) {
    return `${log.timestamp || ''}_${log.service || ''}_${log.level || ''}_${log.message?.substring(0, 20) || ''}`.replace(/\s/g, '_');
  }

  switchDetailsTab(tab) {
    document.querySelectorAll('.server-logs-details-tab').forEach(t => t.classList.remove('active'));
    document.querySelector(`[data-tab="${tab}"]`)?.classList.add('active');
    
    if (!this.state.selectedLog) return;
    
    const content = document.getElementById('sl-details-content');
    if (!content) return;

    switch(tab) {
      case 'raw':
        content.innerHTML = `<pre class="whitespace-pre-wrap break-words">${this.escapeHtml(this.state.selectedLog.raw || '')}</pre>`;
        break;
      case 'json':
        content.innerHTML = `<pre class="whitespace-pre-wrap break-words">${this.escapeHtml(JSON.stringify(this.state.selectedLog, null, 2))}</pre>`;
        break;
      case 'context':
        content.innerHTML = `<div class="sl-text-xs sl-text-secondary">Context view (previous/next logs) would go here</div>`;
        break;
    }
  }

  toggleService(service) {
    if (this.state.filters.services.includes(service)) {
      this.state.filters.services = this.state.filters.services.filter(s => s !== service);
    } else {
      this.state.filters.services.push(service);
    }
    this.loadLogs();
  }

  toggleLevel(level) {
    if (this.state.filters.levels.includes(level)) {
      this.state.filters.levels = this.state.filters.levels.filter(l => l !== level);
    } else {
      this.state.filters.levels.push(level);
    }
    this.loadLogs();
  }

  selectLog(log) {
    this.state.selectedLog = log;
    this.state.detailsCollapsed = false;
    this.renderDetails();
  }

  renderDetails() {
    if (!this.state.selectedLog) {
      const details = document.getElementById('sl-details');
      if (details) {
        details.classList.add('collapsed');
      }
      return;
    }

    const log = this.state.selectedLog;
    const details = document.getElementById('sl-details');
    if (!details) return;

    details.classList.remove('collapsed');
    
    const levelEl = document.getElementById('sl-details-level');
    const serviceEl = document.getElementById('sl-details-service');
    const timestampEl = document.getElementById('sl-details-timestamp');
    
    if (levelEl) levelEl.textContent = log.level || 'INFO';
    if (serviceEl) serviceEl.textContent = log.service || 'unknown';
    if (timestampEl) timestampEl.textContent = log.timestamp || '';

    // Set active tab
    const activeTab = document.querySelector('.server-logs-details-tab.active')?.dataset.tab || 'raw';
    this.switchDetailsTab(activeTab);
  }

  updateSearchMatches() {
    if (!this.state.filters.search) {
      this.state.searchMatches = { total: 0, current: 0, indices: [] };
      return;
    }

    const indices = [];
    let regex;

    try {
      if (this.state.filters.searchRegex) {
        regex = new RegExp(this.state.filters.search, 'gi');
      } else {
        regex = new RegExp(this.escapeRegex(this.state.filters.search), 'gi');
      }
    } catch (e) {
      // Invalid regex
      this.state.searchMatches = { total: 0, current: 0, indices: [] };
      return;
    }

    this.state.logs.forEach((log, index) => {
      const text = `${log.message || ''} ${log.raw || ''} ${log.service || ''}`;
      if (regex.test(text)) {
        indices.push(index);
      }
    });

    this.state.searchMatches = {
      total: indices.length,
      current: indices.length > 0 ? 1 : 0,
      indices: indices
    };
  }

  navigateSearch(direction) {
    if (this.state.searchMatches.total === 0) return;

    let newCurrent = this.state.searchMatches.current + direction;
    if (newCurrent < 1) newCurrent = this.state.searchMatches.total;
    if (newCurrent > this.state.searchMatches.total) newCurrent = 1;

    this.state.searchMatches.current = newCurrent;
    this.render();

    // Scroll to current match
    setTimeout(() => {
      const currentIndex = this.state.searchMatches.indices[newCurrent - 1];
      if (currentIndex !== undefined && this.state.logs[currentIndex]) {
        const log = this.state.logs[currentIndex];
        const logId = this.getLogId(log);
        // Find element by matching logId (need to check all elements due to HTML escaping)
        const allLogElements = document.querySelectorAll('[data-log-id]');
        let logElement = null;
        
        for (let el of allLogElements) {
          const elLogId = el.dataset.logId.replace(/&quot;/g, '"').replace(/&#39;/g, "'");
          if (elLogId === logId) {
            logElement = el;
            break;
          }
        }
        
        if (logElement) {
          logElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }
    }, 100);
  }

  handleTableSort(column) {
    if (this.state.tableSort.column === column) {
      // Toggle direction
      this.state.tableSort.direction = this.state.tableSort.direction === 'asc' ? 'desc' : 'asc';
    } else {
      // New column, default to asc
      this.state.tableSort.column = column;
      this.state.tableSort.direction = 'asc';
    }
    this.render();
  }

  // Phase 3: Pin/Unpin log
  togglePinLog(log) {
    const logId = this.getLogId(log);
    if (this.state.pinnedLogs.has(logId)) {
      this.state.pinnedLogs.delete(logId);
    } else {
      this.state.pinnedLogs.add(logId);
    }
    this.savePinnedLogs();
    this.render();
  }

  // Phase 3: Expand/Collapse log
  toggleExpandLog(log) {
    const logId = this.getLogId(log);
    if (this.state.expandedLogs.has(logId)) {
      this.state.expandedLogs.delete(logId);
    } else {
      this.state.expandedLogs.add(logId);
    }
    this.render();
  }

  // Phase 3: Copy log content
  async copyLog(log, type) {
    let text = '';
    
    switch(type) {
      case 'raw':
        text = log.raw || '';
        break;
      case 'json':
        text = JSON.stringify(log, null, 2);
        break;
      case 'stack':
        text = this.extractStackTrace(log) || 'No stack trace found';
        break;
      default:
        text = log.raw || '';
    }

    try {
      await navigator.clipboard.writeText(text);
      this.showToast(`Copied ${type} to clipboard`);
    } catch (e) {
      console.error('Failed to copy:', e);
      // Fallback for older browsers
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      try {
        document.execCommand('copy');
        this.showToast(`Copied ${type} to clipboard`);
      } catch (e2) {
        this.showToast('Failed to copy', 'error');
      }
      document.body.removeChild(textarea);
    }
  }

  // Phase 3: Jump to next error
  jumpToNextError() {
    if (this.state.errorIndices.length === 0) {
      this.showToast('No errors found', 'info');
      return;
    }

    // Find current position
    const viewer = document.getElementById('sl-viewer');
    if (!viewer) return;

    const visibleLogs = Array.from(viewer.querySelectorAll('[data-log-id]'));
    let currentIndex = -1;

    // Find first visible log index
    for (let i = 0; i < visibleLogs.length; i++) {
      let logId = visibleLogs[i].dataset.logId;
      // Decode HTML entities
      if (logId) {
        logId = logId.replace(/&quot;/g, '"').replace(/&#39;/g, "'");
      }
      const logIndex = this.state.logs.findIndex(l => this.getLogId(l) === logId);
      if (logIndex >= 0) {
        currentIndex = logIndex;
        break;
      }
    }

    // Find next error after current position
    const nextErrorIndex = this.state.errorIndices.find(idx => idx > currentIndex);
    const targetIndex = nextErrorIndex !== undefined ? nextErrorIndex : this.state.errorIndices[0];

    if (targetIndex !== undefined && this.state.logs[targetIndex]) {
      const log = this.state.logs[targetIndex];
      const logId = this.getLogId(log);
      // Find element by matching logId (need to check all elements)
      const allLogElements = document.querySelectorAll('[data-log-id]');
      let logElement = null;
      
      for (let el of allLogElements) {
        // Decode HTML entities for comparison
        const elLogId = el.dataset.logId.replace(/&quot;/g, '"').replace(/&#39;/g, "'");
        if (elLogId === logId) {
          logElement = el;
          break;
        }
      }
      
      if (logElement) {
        logElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
        // Highlight briefly
        logElement.style.transition = 'background-color 0.3s';
        logElement.style.backgroundColor = 'rgba(220, 38, 38, 0.2)';
        setTimeout(() => {
          logElement.style.backgroundColor = '';
        }, 1000);
      }
    }
  }

  // Phase 3: Export logs
  showExportMenu(e) {
    // Phase 4: Remove existing menu if any
    const existingMenu = document.querySelector('.sl-export-menu');
    if (existingMenu) {
      document.body.removeChild(existingMenu);
    }

    // Simple dropdown menu
    const menu = document.createElement('div');
    menu.className = 'sl-export-menu absolute bg-[var(--sl-bg-elevated)] border border-[var(--sl-border-muted)] rounded-lg shadow-lg p-2 z-50 sl-fade-in';
    const rect = e.target.getBoundingClientRect();
    menu.style.top = `${rect.bottom + 5}px`;
    menu.style.right = `${window.innerWidth - rect.right}px`;
    
    menu.innerHTML = `
      <button class="w-full text-left px-3 py-2 sl-text-xs hover:bg-[var(--sl-bg-hover)] rounded" data-export="json">
        <i class="fas fa-file-code mr-2"></i>Export as JSON
      </button>
      <button class="w-full text-left px-3 py-2 sl-text-xs hover:bg-[var(--sl-bg-hover)] rounded" data-export="csv">
        <i class="fas fa-file-csv mr-2"></i>Export as CSV
      </button>
      <button class="w-full text-left px-3 py-2 sl-text-xs hover:bg-[var(--sl-bg-hover)] rounded" data-export="txt">
        <i class="fas fa-file-alt mr-2"></i>Export as TXT
      </button>
    `;

    menu.addEventListener('click', (e) => {
      const format = e.target.closest('[data-export]')?.dataset.export;
      if (format) {
        this.exportLogs(format);
        document.body.removeChild(menu);
      }
    });

    // Close on outside click
    const closeMenu = (e) => {
      if (!menu.contains(e.target) && e.target.id !== 'sl-export-btn' && !e.target.closest('#sl-export-btn')) {
        document.body.removeChild(menu);
        document.removeEventListener('click', closeMenu);
      }
    };
    setTimeout(() => document.addEventListener('click', closeMenu), 0);

    document.body.appendChild(menu);
  }

  exportLogs(format) {
    const logs = this.state.logs;
    let content = '';
    let filename = '';
    let mimeType = '';

    switch(format) {
      case 'json':
        content = JSON.stringify(logs, null, 2);
        filename = `server-logs-${new Date().toISOString().split('T')[0]}.json`;
        mimeType = 'application/json';
        break;
      case 'csv':
        const headers = ['Timestamp', 'Level', 'Service', 'Message'];
        const rows = logs.map(log => [
          log.timestamp || '',
          log.level || '',
          log.service || '',
          (log.message || '').replace(/"/g, '""') // Escape quotes
        ]);
        content = [
          headers.join(','),
          ...rows.map(row => row.map(cell => `"${cell}"`).join(','))
        ].join('\n');
        filename = `server-logs-${new Date().toISOString().split('T')[0]}.csv`;
        mimeType = 'text/csv';
        break;
      case 'txt':
        content = logs.map(log => log.raw || `${log.timestamp || ''} ${log.level || ''} ${log.service || ''} ${log.message || ''}`).join('\n');
        filename = `server-logs-${new Date().toISOString().split('T')[0]}.txt`;
        mimeType = 'text/plain';
        break;
      default:
        return;
    }

    // Create download
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    this.showToast(`Exported ${logs.length} logs as ${format.toUpperCase()}`);
  }

  // Phase 4: Toast notification (improved)
  showToast(message, type = 'success') {
    // Remove existing toasts
    const existingToasts = document.querySelectorAll('.sl-toast');
    existingToasts.forEach(t => {
      t.style.opacity = '0';
      setTimeout(() => {
        if (t.parentNode) document.body.removeChild(t);
      }, 300);
    });

    const toast = document.createElement('div');
    toast.className = `sl-toast fixed bottom-4 right-4 px-4 py-3 rounded-lg shadow-lg z-50 sl-text-sm font-medium sl-slide-in-right`;
    
    const colors = {
      success: 'bg-[var(--sl-accent-blue)] text-white',
      error: 'bg-[var(--sl-color-error)] text-white',
      info: 'bg-[var(--sl-bg-elevated)] border border-[var(--sl-border-default)] text-[var(--sl-text-primary)]'
    };
    
    toast.className += ` ${colors[type] || colors.success}`;
    
    const icon = {
      success: 'fa-check-circle',
      error: 'fa-exclamation-circle',
      info: 'fa-info-circle'
    }[type] || 'fa-check-circle';
    
    toast.innerHTML = `
      <div class="flex items-center gap-2">
        <i class="fas ${icon}"></i>
        <span>${message}</span>
      </div>
    `;
    
    document.body.appendChild(toast);
    
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(20px)';
      setTimeout(() => {
        if (toast.parentNode) {
          document.body.removeChild(toast);
        }
      }, 300);
    }, 3000);
  }

  // Phase 4: Show keyboard shortcuts
  showKeyboardShortcuts() {
    const modal = document.createElement('div');
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50 sl-fade-in';
    modal.innerHTML = `
      <div class="bg-[var(--sl-bg-elevated)] border border-[var(--sl-border-muted)] rounded-lg shadow-xl max-w-md w-full mx-4 sl-slide-in-right">
        <div class="px-6 py-4 border-b border-[var(--sl-border-muted)] flex items-center justify-between">
          <h3 class="sl-text-base font-semibold sl-text-primary">Keyboard Shortcuts</h3>
          <button id="sl-shortcuts-close" class="p-1 hover:bg-[var(--sl-bg-hover)] rounded text-[var(--sl-text-secondary)]">
            <i class="fas fa-times"></i>
          </button>
        </div>
        <div class="px-6 py-4 space-y-3">
          <div class="flex items-center justify-between">
            <span class="sl-text-sm sl-text-secondary">Focus search</span>
            <kbd class="px-2 py-1 bg-[var(--sl-bg-input)] border border-[var(--sl-border-default)] rounded sl-text-xs sl-font-mono">/</kbd>
          </div>
          <div class="flex items-center justify-between">
            <span class="sl-text-sm sl-text-secondary">Navigate down</span>
            <div class="flex gap-1">
              <kbd class="px-2 py-1 bg-[var(--sl-bg-input)] border border-[var(--sl-border-default)] rounded sl-text-xs sl-font-mono">j</kbd>
              <span class="sl-text-xs sl-text-tertiary">or</span>
              <kbd class="px-2 py-1 bg-[var(--sl-bg-input)] border border-[var(--sl-border-default)] rounded sl-text-xs sl-font-mono">↓</kbd>
            </div>
          </div>
          <div class="flex items-center justify-between">
            <span class="sl-text-sm sl-text-secondary">Navigate up</span>
            <div class="flex gap-1">
              <kbd class="px-2 py-1 bg-[var(--sl-bg-input)] border border-[var(--sl-border-default)] rounded sl-text-xs sl-font-mono">k</kbd>
              <span class="sl-text-xs sl-text-tertiary">or</span>
              <kbd class="px-2 py-1 bg-[var(--sl-bg-input)] border border-[var(--sl-border-default)] rounded sl-text-xs sl-font-mono">↑</kbd>
            </div>
          </div>
          <div class="flex items-center justify-between">
            <span class="sl-text-sm sl-text-secondary">Next search match</span>
            <kbd class="px-2 py-1 bg-[var(--sl-bg-input)] border border-[var(--sl-border-default)] rounded sl-text-xs sl-font-mono">Ctrl+N</kbd>
          </div>
          <div class="flex items-center justify-between">
            <span class="sl-text-sm sl-text-secondary">Previous search match</span>
            <kbd class="px-2 py-1 bg-[var(--sl-bg-input)] border border-[var(--sl-border-default)] rounded sl-text-xs sl-font-mono">Ctrl+P</kbd>
          </div>
          <div class="flex items-center justify-between">
            <span class="sl-text-sm sl-text-secondary">Jump to error</span>
            <kbd class="px-2 py-1 bg-[var(--sl-bg-input)] border border-[var(--sl-border-default)] rounded sl-text-xs sl-font-mono">Ctrl+E</kbd>
          </div>
          <div class="flex items-center justify-between">
            <span class="sl-text-sm sl-text-secondary">Close / Clear</span>
            <kbd class="px-2 py-1 bg-[var(--sl-bg-input)] border border-[var(--sl-border-default)] rounded sl-text-xs sl-font-mono">Esc</kbd>
          </div>
        </div>
      </div>
    `;

    modal.addEventListener('click', (e) => {
      if (e.target.id === 'sl-shortcuts-close' || e.target === modal) {
        modal.style.opacity = '0';
        setTimeout(() => {
          if (modal.parentNode) {
            document.body.removeChild(modal);
          }
        }, 300);
      }
    });

    document.body.appendChild(modal);
  }

  async loadLogs() {
    this.state.loading = true;
    this.state.loadingProgress = 0;
    this.state.error = null;
    this.renderViewer();

    // Phase 4: Simulate progress for better UX
    let progressInterval = setInterval(() => {
      if (this.state.loadingProgress < 90) {
        this.state.loadingProgress += 10;
        const viewer = document.getElementById('sl-viewer');
        if (viewer && this.state.loading) {
          const progressBar = viewer.querySelector('.h-1');
          if (progressBar) {
            progressBar.style.width = `${this.state.loadingProgress}%`;
          }
        }
      }
    }, 100);

    try {
      const params = new URLSearchParams({
        lines: 1000,
        service: this.state.filters.services.length === 3 ? 'all' : (this.state.filters.services.length > 0 ? this.state.filters.services.join(',') : 'all'),
        level: this.state.filters.levels.length === 4 ? 'all' : (this.state.filters.levels.length > 0 ? this.state.filters.levels[0] : 'all'),
        search: this.state.filters.search
      });

      const response = await fetch(`${this.apiUrl}/api/server_logs?${params}`);
      
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      
      const data = await response.json();

      if (data.success) {
        // Filter logs client-side based on selected services and levels
        let filteredLogs = data.logs || [];
        
        // Filter by services
        if (this.state.filters.services.length < 3) {
          filteredLogs = filteredLogs.filter(log => 
            this.state.filters.services.includes(log.service)
          );
        }
        
        // Filter by levels
        if (this.state.filters.levels.length < 4) {
          filteredLogs = filteredLogs.filter(log => {
            const logLevel = (log.level || 'INFO').toLowerCase();
            return this.state.filters.levels.some(level => 
              logLevel.includes(level.toLowerCase())
            );
          });
        }
        
        // Filter by search
        if (this.state.filters.search) {
          const searchLower = this.state.filters.search.toLowerCase();
          filteredLogs = filteredLogs.filter(log => 
            (log.message || '').toLowerCase().includes(searchLower) ||
            (log.raw || '').toLowerCase().includes(searchLower) ||
            (log.service || '').toLowerCase().includes(searchLower)
          );
        }
        
        // При изменении фильтров сбрасываем счетчик отрисованных логов
        const filtersChanged = JSON.stringify(this.state.filters) !== JSON.stringify(this._lastFilters);
        if (filtersChanged) {
          this.state.lastRenderedLogCount = 0;
          this._lastFilters = JSON.parse(JSON.stringify(this.state.filters));
        }
        
        this.state.logs = filteredLogs;
        this.state.allLogs = filteredLogs; // For virtual scrolling
        
        // Phase 4: Clear memoization cache when logs change
        this.clearMemoization();
        
        // Calculate stats from filtered logs
        this.state.stats = {
          error: filteredLogs.filter(l => (l.level || '').toUpperCase().includes('ERROR')).length,
          warning: filteredLogs.filter(l => (l.level || '').toUpperCase().includes('WARN')).length,
          info: filteredLogs.filter(l => (l.level || '').toUpperCase() === 'INFO').length,
          debug: filteredLogs.filter(l => (l.level || '').toUpperCase() === 'DEBUG').length
        };

        // Phase 3: Update error indices for jump navigation
        this.state.errorIndices = [];
        filteredLogs.forEach((log, index) => {
          if ((log.level || '').toUpperCase().includes('ERROR')) {
            this.state.errorIndices.push(index);
          }
        });

        // Update search matches after loading
        this.updateSearchMatches();
        
        // Auto-scroll to bottom to show newest logs (xterm handles this automatically)
        if (__serverLogsTerminal) {
          setTimeout(() => {
            if (__serverLogsTerminal) {
              __serverLogsTerminal.scrollToBottom();
            }
          }, 100);
        } else {
          setTimeout(() => {
            const viewer = document.getElementById('sl-viewer');
            if (viewer) {
              viewer.scrollTop = viewer.scrollHeight;
            }
          }, 100);
        }
      } else {
        this.state.error = data.error || 'Unknown error';
      }
    } catch (error) {
      this.state.error = error.message || 'Failed to load logs';
      console.error('Error loading logs:', error);
    } finally {
      clearInterval(progressInterval);
      this.state.loading = false;
      this.state.loadingProgress = 100;
      this.render();
    }
  }

  getLevelColor(level) {
    const colors = {
      'ERROR': 'var(--sl-color-error)',
      'WARNING': 'var(--sl-color-warn)',
      'WARN': 'var(--sl-color-warn)',
      'INFO': 'var(--sl-color-info)',
      'DEBUG': 'var(--sl-color-debug)'
    };
    return colors[level] || colors['INFO'];
  }

  formatTime(timestamp) {
    if (!timestamp) return '--:--:--';
    const match = timestamp.match(/(\d{2}:\d{2}:\d{2})/);
    return match ? match[1] : timestamp.split(' ')[1] || '--:--:--';
  }

  escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // Phase 4: Debounce utility
  debounce(key, fn, delay) {
    if (this.debounceTimers[key]) {
      clearTimeout(this.debounceTimers[key]);
    }
    this.debounceTimers[key] = setTimeout(fn, delay);
  }

  // Phase 4: Memoization utility
  memoize(key, fn) {
    if (this.memoizedResults.has(key)) {
      return this.memoizedResults.get(key);
    }
    const result = fn();
    this.memoizedResults.set(key, result);
    return result;
  }

  // Phase 4: Clear memoization cache
  clearMemoization() {
    this.memoizedResults.clear();
  }

  // Phase 4: Load more logs (for virtual scrolling)
  loadMoreLogs() {
    const currentLimit = this.state.virtualScroll.endIndex || 100;
    this.state.virtualScroll.endIndex = Math.min(
      this.state.logs.length,
      currentLimit + 500
    );
    this.render();
  }

  destroy() {
    if (this.autoRefreshInterval) {
      clearInterval(this.autoRefreshInterval);
    }
    // Clear all timers
    Object.values(this.debounceTimers).forEach(timer => clearTimeout(timer));
    this.debounceTimers = {};
    this.clearMemoization();
    
    // Remove scroll listener
    const viewer = document.getElementById('sl-viewer');
    if (viewer && this._scrollHandler) {
      viewer.removeEventListener('scroll', this._scrollHandler);
    }
    
    // Cleanup xterm
    if (__serverLogsTerminalMeta?.resizeObserver) {
      __serverLogsTerminalMeta.resizeObserver.disconnect();
    }
    if (__serverLogsTerminal) {
      try {
        __serverLogsTerminal.dispose();
      } catch (e) {
        console.warn('[ServerLogs] Error disposing terminal:', e);
      }
      __serverLogsTerminal = null;
    }
    __serverLogsTerminalMeta = null;
  }
}

// Initialize when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('server-logs-app')) {
      window.serverLogsApp = new ServerLogsApp('server-logs-app');
    }
  });
} else {
  if (document.getElementById('server-logs-app')) {
    window.serverLogsApp = new ServerLogsApp('server-logs-app');
  }
}
