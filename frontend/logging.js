/**
 * Frontend Logging Module
 * 
 * Система логирования для frontend, аналогичная backend и worker.
 * Логи отправляются на backend через API и записываются в data/logs/frontend.log
 */

(function() {
    'use strict';

    /**
     * Resolve backend base URL (same logic as main app).
     * When frontend is served from :8080 and backend from :5000, relative `/api/...`
     * would hit the frontend server and produce noisy 404/501 errors.
     */
    function getApiBaseUrl() {
        try {
            const meta = document.querySelector('meta[name="api-url"]')?.content;
            if (meta && String(meta).trim()) return String(meta).trim().replace(/\/+$/, '');
        } catch (_) {}
        try {
            if (window.API_URL && String(window.API_URL).trim()) return String(window.API_URL).trim().replace(/\/+$/, '');
        } catch (_) {}
        const origin = window.location.origin;
        if (origin.includes(':8080')) return origin.replace(':8080', ':5000');
        return origin;
    }

    function getApiUrl(path) {
        const base = getApiBaseUrl();
        const p = String(path || '');
        if (p.startsWith('http://') || p.startsWith('https://')) return p;
        return base.replace(/\/+$/, '') + (p.startsWith('/') ? p : '/' + p);
    }

    // Конфигурация
    const CONFIG = {
        apiEndpoint: '/api/frontend_logs',
        batchInterval: 5000, // Отправка батчами каждые 5 секунд
        batchSize: 50, // Максимальный размер батча
        maxBufferSize: 1000, // Максимальный размер буфера (защита от переполнения)
        retryAttempts: 3, // Количество попыток при ошибке
        retryDelay: 1000, // Задержка между попытками (мс)
        logLevel: 'INFO', // Уровень по умолчанию (будет загружен из настроек)
        settingsPollInterval: 30000 // Polling настроек каждые 30 секунд
    };

    // Уровни логирования (аналогично Python logging)
    const LOG_LEVELS = {
        DEBUG: 10,
        INFO: 20,
        WARNING: 30,
        ERROR: 40,
        CRITICAL: 50
    };

    // Состояние модуля
    const state = {
        buffer: [], // Буфер логов для batch отправки
        batchTimer: null, // Таймер для batch отправки
        settingsPollTimer: null, // Таймер для polling настроек
        isInitialized: false,
        logLevel: LOG_LEVELS.INFO, // Текущий уровень логирования
        originalConsole: {}, // Оригинальные методы console
        skipSendUntil: 0, // Не отправлять логи до времени (мс), если был 401
        context: {} // Глобальный контекст (project_id, user_id и т.д.)
    };

    /**
     * Форматирует timestamp в формат [YYYY-MM-DD HH:MM:SS]
     */
    function formatTimestamp() {
        const now = new Date();
        const year = now.getFullYear();
        const month = String(now.getMonth() + 1).padStart(2, '0');
        const day = String(now.getDate()).padStart(2, '0');
        const hours = String(now.getHours()).padStart(2, '0');
        const minutes = String(now.getMinutes()).padStart(2, '0');
        const seconds = String(now.getSeconds()).padStart(2, '0');
        return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
    }

    /**
     * Форматирует контекст в строку: | key1=value1 | key2=value2
     */
    function formatContext(context) {
        if (!context || Object.keys(context).length === 0) {
            return '';
        }
        
        const parts = [];
        for (const [key, value] of Object.entries(context)) {
            if (value !== null && value !== undefined) {
                parts.push(`${key}=${value}`);
            }
        }
        
        return parts.length > 0 ? ' | ' + parts.join(' | ') : '';
    }

    /**
     * Извлекает project_id из URL или localStorage
     */
    function extractProjectId() {
        // Пробуем извлечь из URL (например, /project/{project_id}/...)
        const urlMatch = window.location.pathname.match(/\/project\/([a-f0-9-]+)/i);
        if (urlMatch) {
            return urlMatch[1];
        }
        
        // Пробуем извлечь из localStorage
        try {
            const storedProject = localStorage.getItem('currentProjectId') || 
                                 localStorage.getItem('selectedProjectId') ||
                                 localStorage.getItem('project_id');
            if (storedProject) {
                return storedProject;
            }
        } catch (e) {
            // Игнорируем ошибки доступа к localStorage
        }
        
        return null;
    }

    /**
     * Извлекает execution_id из URL или localStorage
     */
    function extractExecutionId() {
        // Пробуем извлечь из URL (например, /execution/{execution_id}/...)
        const urlMatch = window.location.pathname.match(/\/execution\/([a-f0-9-]+)/i);
        if (urlMatch) {
            return urlMatch[1];
        }
        
        // Пробуем извлечь из localStorage
        try {
            const storedExecution = localStorage.getItem('currentExecutionId') || 
                                   localStorage.getItem('selectedExecutionId') ||
                                   localStorage.getItem('execution_id');
            if (storedExecution) {
                return storedExecution;
            }
        } catch (e) {
            // Игнорируем ошибки доступа к localStorage
        }
        
        return null;
    }

    /**
     * Извлекает user_id из localStorage или других источников
     */
    function extractUserId() {
        try {
            const storedUser = localStorage.getItem('userId') || 
                              localStorage.getItem('user_id') ||
                              localStorage.getItem('currentUserId');
            if (storedUser) {
                return storedUser;
            }
        } catch (e) {
            // Игнорируем ошибки доступа к localStorage
        }
        
        return null;
    }

    /**
     * Собирает автоматический контекст (url, user_agent, project_id, execution_id и т.д.)
     */
    function getAutoContext() {
        const autoContext = {
            url: window.location.href,
            user_agent: navigator.userAgent
        };
        
        // Извлекаем project_id, execution_id, user_id если доступны
        const projectId = extractProjectId();
        if (projectId) {
            autoContext.project_id = projectId;
        }
        
        const executionId = extractExecutionId();
        if (executionId) {
            autoContext.execution_id = executionId;
        }
        
        const userId = extractUserId();
        if (userId) {
            autoContext.user_id = userId;
        }
        
        // Добавляем глобальный контекст если есть (имеет приоритет над автоматически извлеченным)
        Object.assign(autoContext, state.context);
        
        return autoContext;
    }

    /**
     * Проверяет, должен ли лог быть отправлен (по уровню логирования)
     */
    function shouldLog(level) {
        return LOG_LEVELS[level] >= state.logLevel;
    }

    /**
     * Создает запись лога
     */
    function createLogEntry(level, message, context, stack) {
        const timestamp = formatTimestamp();
        const autoContext = getAutoContext();
        const mergedContext = Object.assign({}, autoContext, context || {});
        const contextStr = formatContext(mergedContext);
        
        return {
            level: level,
            message: message + contextStr,
            timestamp: timestamp,
            context: mergedContext,
            stack: stack || null
        };
    }

    /**
     * Добавляет лог в буфер для batch отправки
     */
    function addToBuffer(logEntry) {
        // Защита от переполнения буфера
        if (state.buffer.length >= CONFIG.maxBufferSize) {
            // Удаляем самые старые логи
            state.buffer.shift();
        }
        
        state.buffer.push(logEntry);
        
        // Запускаем batch отправку если буфер достиг размера
        if (state.buffer.length >= CONFIG.batchSize) {
            sendBatch();
        } else if (!state.batchTimer) {
            // Запускаем таймер для batch отправки
            state.batchTimer = setTimeout(sendBatch, CONFIG.batchInterval);
        }
    }

    /**
     * Отправляет батч логов на backend
     */
    async function sendBatch() {
        if (state.buffer.length === 0) {
            if (state.batchTimer) {
                clearTimeout(state.batchTimer);
                state.batchTimer = null;
            }
            return;
        }
        if (state.skipSendUntil && Date.now() < state.skipSendUntil) {
            return;
        }

        // Копируем буфер и очищаем его
        const logsToSend = state.buffer.slice();
        const logsCount = logsToSend.length;
        state.buffer = [];
        
        if (state.batchTimer) {
            clearTimeout(state.batchTimer);
            state.batchTimer = null;
        }

        // Отправляем с retry
        let attempt = 0;
        while (attempt < CONFIG.retryAttempts) {
            try {
                state.originalConsole.debug(`[FrontendLogger] Sending batch of ${logsCount} logs to ${CONFIG.apiEndpoint}`);
                
                const response = await fetch(getApiUrl(CONFIG.apiEndpoint), {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        logs: logsToSend
                    })
                });

                if (response.ok) {
                    const result = await response.json();
                    // Успешно отправлено
                    if (result.logged !== undefined) {
                        state.originalConsole.debug(`[FrontendLogger] Sent ${result.logged}/${logsCount} logs to backend`);
                        if (result.logged < logsCount) {
                            state.originalConsole.warn(`[FrontendLogger] Only ${result.logged} of ${logsCount} logs were logged (some may be filtered by log level)`);
                        }
                    }
                    return;
                }
                if (response.status === 401) {
                    // Не авторизован — не повторяем, не спамим в консоль; возвращаем логи в буфер
                    state.skipSendUntil = Date.now() + 60000;
                    state.buffer = logsToSend.concat(state.buffer);
                    return;
                }
                const errorText = await response.text().catch(() => response.statusText);
                throw new Error(`HTTP ${response.status}: ${errorText}`);
                } catch (error) {
                if (error.message && error.message.indexOf('401') !== -1) {
                    state.skipSendUntil = Date.now() + 60000;
                    state.buffer = logsToSend.concat(state.buffer);
                    return;
                }
                attempt++;
                if (attempt >= CONFIG.retryAttempts) {
                    // Все попытки исчерпаны
                    // Логируем в console (fallback) - это не блокирует выполнение
                    try {
                        state.originalConsole.error(`[FrontendLogger] Failed to send ${logsCount} logs after ${CONFIG.retryAttempts} attempts:`, error);
                    } catch (e) {
                        // Даже console.error может не работать в некоторых случаях
                        // Игнорируем ошибку, чтобы не блокировать выполнение
                    }
                    
                    // Возвращаем логи в буфер (но не все, чтобы не переполнить)
                    // Оставляем только последние N логов, чтобы не переполнить буфер
                    const logsToKeep = logsToSend.slice(-Math.min(CONFIG.batchSize, CONFIG.maxBufferSize / 2));
                    state.buffer = logsToKeep.concat(state.buffer);
                    
                    // Если буфер все еще переполнен, удаляем самые старые логи
                    if (state.buffer.length > CONFIG.maxBufferSize) {
                        const excess = state.buffer.length - CONFIG.maxBufferSize;
                        state.buffer.splice(0, excess);
                        try {
                            state.originalConsole.warn(`[FrontendLogger] Buffer overflow, dropped ${excess} oldest logs`);
                        } catch (e) {
                            // Игнорируем ошибку console
                        }
                    }
                    
                    // Не бросаем исключение - graceful degradation
                    // Продолжаем работу, даже если не удалось отправить логи
                    return;
                } else {
                    // Ждем перед следующей попыткой (exponential backoff)
                    const delay = CONFIG.retryDelay * Math.pow(2, attempt - 1); // Exponential backoff: 1s, 2s, 4s...
                    try {
                        state.originalConsole.debug(`[FrontendLogger] Retry attempt ${attempt}/${CONFIG.retryAttempts} after ${delay}ms`);
                    } catch (e) {
                        // Игнорируем ошибку console
                    }
                    await new Promise(resolve => setTimeout(resolve, delay));
                }
            }
        }
    }

    /**
     * Основная функция логирования
     */
    function log(level, message, context, exception) {
        // Проверяем, что модуль инициализирован
        if (!state.isInitialized) {
            // Если модуль еще не инициализирован, просто логируем в console
            try {
                if (level === 'ERROR' || level === 'CRITICAL') {
                    console.error(`[FrontendLogger not initialized] ${level}: ${message}`, exception || '');
                } else {
                    console.log(`[FrontendLogger not initialized] ${level}: ${message}`);
                }
            } catch (e) {
                // Игнорируем ошибки console
            }
            return;
        }
        
        // Проверяем уровень логирования
        if (!shouldLog(level)) {
            return;
        }

        // Валидация и санитизация сообщения
        if (!message || typeof message !== 'string') {
            message = String(message || '');
        }
        
        // Ограничиваем длину сообщения (защита от очень длинных сообщений)
        const MAX_MESSAGE_LENGTH = 10000;
        if (message.length > MAX_MESSAGE_LENGTH) {
            message = message.substring(0, MAX_MESSAGE_LENGTH) + '...[truncated]';
        }

        // Создаем запись лога
        let stack = null;
        if (exception) {
            if (exception instanceof Error) {
                stack = exception.stack || exception.toString();
                // Ограничиваем длину stack trace
                if (stack && stack.length > MAX_MESSAGE_LENGTH) {
                    stack = stack.substring(0, MAX_MESSAGE_LENGTH) + '...[truncated]';
                }
            } else {
                stack = String(exception);
                if (stack.length > MAX_MESSAGE_LENGTH) {
                    stack = stack.substring(0, MAX_MESSAGE_LENGTH) + '...[truncated]';
                }
            }
        }

        // Валидация контекста (удаляем циклические ссылки и большие объекты)
        let safeContext = null;
        if (context && typeof context === 'object') {
            try {
                // Пытаемся сериализовать для проверки на циклические ссылки
                JSON.stringify(context);
                safeContext = context;
            } catch (e) {
                // Если есть циклические ссылки, создаем упрощенный контекст
                safeContext = {};
                for (const [key, value] of Object.entries(context)) {
                    if (value !== null && typeof value !== 'object') {
                        safeContext[key] = value;
                    } else if (value !== null) {
                        safeContext[key] = '[object]';
                    }
                }
            }
        }

        const logEntry = createLogEntry(level, message, safeContext, stack);
        
        // Добавляем в буфер (не блокируем выполнение при ошибках)
        try {
            addToBuffer(logEntry);
        } catch (error) {
            // Graceful degradation: если не удалось добавить в буфер, просто логируем в console
            try {
                state.originalConsole.error('[FrontendLogger] Failed to add log to buffer:', error);
            } catch (e) {
                // Игнорируем ошибку console
            }
        }
    }

    /**
     * Публичные функции логирования
     */
    const FrontendLogger = {
        /**
         * Инициализация модуля логирования
         */
        init: async function() {
            if (state.isInitialized) {
                return;
            }

            try {
                // Сохраняем оригинальные методы console
                state.originalConsole.log = console.log.bind(console);
                state.originalConsole.info = console.info.bind(console);
                state.originalConsole.warn = console.warn.bind(console);
                state.originalConsole.error = console.error.bind(console);
                state.originalConsole.debug = console.debug.bind(console);

                // Загружаем настройки логирования
                await this.loadSettings();

                // Запускаем polling для обновления настроек
                this.startSettingsPolling();

                // Перехватываем console методы
                this.interceptConsole();

                // Устанавливаем обработчики глобальных ошибок
                this.setupErrorHandlers();

                state.isInitialized = true;
                state.originalConsole.info('[FrontendLogger] Initialized');
                
                // Отправляем тестовый лог для проверки работы
                this.info('[FrontendLogger] Logging module initialized successfully');
                
                // Принудительно отправляем тестовый лог сразу (не ждем batch)
                setTimeout(() => {
                    this.flush();
                }, 1000);
            } catch (error) {
                state.originalConsole.error('[FrontendLogger] Initialization error:', error);
                // Продолжаем работу даже при ошибке инициализации
            }
        },

        /**
         * Загружает настройки логирования из backend
         */
        loadSettings: async function() {
            try {
                const response = await fetch(getApiUrl('/api/execution_settings'));
                if (response.ok) {
                    const data = await response.json();
                    if (data.log_level) {
                        const level = data.log_level.toUpperCase();
                        if (LOG_LEVELS[level] !== undefined) {
                            const newLevel = LOG_LEVELS[level];
                            if (newLevel !== state.logLevel) {
                                const oldLevel = Object.keys(LOG_LEVELS).find(key => LOG_LEVELS[key] === state.logLevel);
                                state.logLevel = newLevel;
                                state.originalConsole.info(`[FrontendLogger] Log level changed from ${oldLevel} to ${level}`);
                            }
                        }
                    }
                }
            } catch (error) {
                state.originalConsole.warn('[FrontendLogger] Failed to load settings:', error);
            }
        },

        /**
         * Запускает polling для обновления настроек логирования
         */
        startSettingsPolling: function() {
            // Останавливаем предыдущий polling если есть
            if (state.settingsPollTimer) {
                clearInterval(state.settingsPollTimer);
            }
            
            // Запускаем polling
            state.settingsPollTimer = setInterval(() => {
                this.loadSettings();
            }, CONFIG.settingsPollInterval);
        },

        /**
         * Останавливает polling настроек
         */
        stopSettingsPolling: function() {
            if (state.settingsPollTimer) {
                clearInterval(state.settingsPollTimer);
                state.settingsPollTimer = null;
            }
        },

        /**
         * Перехватывает console методы
         */
        interceptConsole: function() {
            const self = this;

            console.log = function(...args) {
                state.originalConsole.log.apply(console, args);
                const message = args.map(arg => {
                    if (typeof arg === 'object') {
                        try {
                            return JSON.stringify(arg);
                        } catch (e) {
                            return String(arg);
                        }
                    }
                    return String(arg);
                }).join(' ');
                self.debug(message);
            };

            console.info = function(...args) {
                state.originalConsole.info.apply(console, args);
                const message = args.map(arg => {
                    if (typeof arg === 'object') {
                        try {
                            return JSON.stringify(arg);
                        } catch (e) {
                            return String(arg);
                        }
                    }
                    return String(arg);
                }).join(' ');
                self.info(message);
            };

            console.warn = function(...args) {
                state.originalConsole.warn.apply(console, args);
                const message = args.map(arg => {
                    if (typeof arg === 'object') {
                        try {
                            return JSON.stringify(arg);
                        } catch (e) {
                            return String(arg);
                        }
                    }
                    return String(arg);
                }).join(' ');
                self.warning(message);
            };

            console.error = function(...args) {
                state.originalConsole.error.apply(console, args);
                const message = args.map(arg => {
                    if (typeof arg === 'object') {
                        try {
                            return JSON.stringify(arg);
                        } catch (e) {
                            return String(arg);
                        }
                    }
                    return String(arg);
                }).join(' ');
                
                // Пытаемся извлечь exception из аргументов
                let exception = null;
                for (const arg of args) {
                    if (arg instanceof Error) {
                        exception = arg;
                        break;
                    }
                }
                
                self.error(message, null, exception);
            };

            console.debug = function(...args) {
                state.originalConsole.debug.apply(console, args);
                const message = args.map(arg => {
                    if (typeof arg === 'object') {
                        try {
                            return JSON.stringify(arg);
                        } catch (e) {
                            return String(arg);
                        }
                    }
                    return String(arg);
                }).join(' ');
                self.debug(message);
            };
        },

        /**
         * Устанавливает обработчики глобальных ошибок
         */
        setupErrorHandlers: function() {
            const self = this;
            
            // Обработка синхронных ошибок
            window.onerror = (message, source, lineno, colno, error) => {
                // Формируем детальное сообщение об ошибке
                let errorMessage = `JavaScript Error: ${message}`;
                if (source) {
                    errorMessage += ` at ${source}`;
                    if (lineno !== undefined) {
                        errorMessage += `:${lineno}`;
                        if (colno !== undefined) {
                            errorMessage += `:${colno}`;
                        }
                    }
                }
                
                // Определяем уровень критичности
                // Синхронные ошибки обычно критичны, но не всегда
                const level = error && error.name === 'ReferenceError' ? 'CRITICAL' : 'ERROR';
                
                // Логируем с контекстом
                const context = {
                    error_type: error ? error.name : 'Unknown',
                    error_source: source || 'unknown',
                    line: lineno || null,
                    column: colno || null
                };
                
                if (level === 'CRITICAL') {
                    this.critical(errorMessage, context, error);
                } else {
                    this.error(errorMessage, context, error);
                }
                
                // Возвращаем false, чтобы браузер мог обработать ошибку стандартным образом
                return false;
            };

            // Обработка необработанных Promise rejections
            window.addEventListener('unhandledrejection', (event) => {
                const reason = event.reason;
                let errorMessage = 'Unhandled Promise Rejection';
                let exception = null;
                let context = {};

                if (reason instanceof Error) {
                    errorMessage = `Unhandled Promise Rejection: ${reason.message}`;
                    exception = reason;
                    context.error_type = reason.name;
                    context.error_stack = reason.stack;
                } else if (typeof reason === 'object' && reason !== null) {
                    // Пытаемся извлечь информацию из объекта
                    errorMessage = `Unhandled Promise Rejection: ${reason.message || reason.error || JSON.stringify(reason)}`;
                    context.rejection_reason = JSON.stringify(reason);
                } else {
                    errorMessage = `Unhandled Promise Rejection: ${String(reason)}`;
                }
                
                // Promise rejections обычно критичны, так как указывают на проблемы в асинхронном коде
                this.critical(errorMessage, context, exception);
                
                // Предотвращаем вывод в консоль браузера (опционально)
                // event.preventDefault();
            }, true); // Используем capture phase для перехвата всех rejections
        },

        /**
         * Устанавливает контекст (project_id, user_id и т.д.)
         */
        setContext: function(context) {
            Object.assign(state.context, context);
        },

        /**
         * Очищает контекст
         */
        clearContext: function() {
            state.context = {};
        },

        /**
         * Логирование DEBUG
         */
        debug: function(message, context) {
            log('DEBUG', message, context, null);
        },

        /**
         * Логирование INFO
         */
        info: function(message, context) {
            log('INFO', message, context, null);
        },

        /**
         * Логирование WARNING
         */
        warning: function(message, context) {
            log('WARNING', message, context, null);
        },

        /**
         * Логирование ERROR
         */
        error: function(message, context, exception) {
            log('ERROR', message, context, exception);
        },

        /**
         * Логирование CRITICAL
         */
        critical: function(message, context, exception) {
            log('CRITICAL', message, context, exception);
        },

        /**
         * Принудительная отправка буфера (для тестирования)
         */
        flush: function() {
            sendBatch();
        }
    };

    // Экспортируем в глобальную область
    window.FrontendLogger = FrontendLogger;

    // Автоматическая инициализация при загрузке DOM
    // Используем немедленную инициализацию, так как скрипт загружается в конце body
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            FrontendLogger.init().catch(err => {
                console.error('[FrontendLogger] Failed to initialize:', err);
            });
        });
    } else {
        // DOM уже загружен
        FrontendLogger.init().catch(err => {
            console.error('[FrontendLogger] Failed to initialize:', err);
        });
    }

})();
