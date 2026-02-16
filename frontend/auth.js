/**
 * Authentication module
 * Handles login, logout, token management, and authentication state
 */

const Auth = {
    // Storage keys
    TOKEN_KEY: 'auth_token',
    REFRESH_TOKEN_KEY: 'auth_refresh_token',
    USER_KEY: 'user_data',
    
    // API base URL (если открыто по IP/хосту, а meta = localhost — используем тот же хост с портом 5000)
    getApiUrl: function() {
        const meta = document.querySelector('meta[name="api-url"]');
        const content = meta ? (meta.getAttribute('content') || '').trim() : '';
        const origin = typeof window !== 'undefined' && window.location ? window.location.origin : '';
        const isLocalhost = /^https?:\/\/localhost(:\d+)?$/i.test(origin);
        const metaIsLocalhost = content && /^https?:\/\/localhost(:\d+)?$/i.test(content);
        if (content && (isLocalhost || !metaIsLocalhost)) return content.replace(/\/$/, '');
        if (!isLocalhost && origin.includes(':8080')) return origin.replace(':8080', ':5000');
        return content || 'http://localhost:5000';
    },
    
    /**
     * Save token to localStorage
     */
    saveToken: function(token, refreshToken = null) {
        if (token) {
            localStorage.setItem(this.TOKEN_KEY, token);
        }
        if (refreshToken) {
            localStorage.setItem(this.REFRESH_TOKEN_KEY, refreshToken);
        }
    },
    
    /**
     * Get token from localStorage
     */
    getToken: function() {
        return localStorage.getItem(this.TOKEN_KEY);
    },
    
    /**
     * Get refresh token from localStorage
     */
    getRefreshToken: function() {
        return localStorage.getItem(this.REFRESH_TOKEN_KEY);
    },
    
    /**
     * Remove tokens from localStorage
     */
    clearTokens: function() {
        localStorage.removeItem(this.TOKEN_KEY);
        localStorage.removeItem(this.REFRESH_TOKEN_KEY);
        localStorage.removeItem(this.USER_KEY);
    },
    
    /**
     * Save user data to localStorage
     */
    saveUser: function(userData) {
        localStorage.setItem(this.USER_KEY, JSON.stringify(userData));
    },
    
    /**
     * Get user data from localStorage
     */
    getUser: function() {
        const userData = localStorage.getItem(this.USER_KEY);
        return userData ? JSON.parse(userData) : null;
    },
    
    /**
     * Check if user is authenticated
     */
    isAuthenticated: function() {
        return !!this.getToken();
    },
    
    /**
     * Login user
     * @param {string} username
     * @param {string} password
     * @returns {Promise<Object>}
     */
    login: function(username, password) {
        return new Promise((resolve, reject) => {
            $.ajax({
                url: `${this.getApiUrl()}/api/auth/login`,
                method: 'POST',
                contentType: 'application/json',
                data: JSON.stringify({
                    username: username,
                    password: password
                }),
                success: (response) => {
                    console.log('[Auth.login] Response received:', response);
                    if (response.access_token) {
                        console.log('[Auth.login] Saving tokens...');
                        this.saveToken(response.access_token, response.refresh_token);
                        console.log('[Auth.login] Token saved, verifying:', this.getToken() ? 'Token exists' : 'Token missing');
                        // Save user data from login response if available
                        if (response.user) {
                            console.log('[Auth.login] Saving user data from login response:', response.user);
                            this.saveUser(response.user);
                            // Update avatar immediately if function is available
                            if (typeof updateUserAvatar === 'function') {
                                updateUserAvatar(response.user);
                            }
                            // Initialize permissions if available (wait for module to load)
                            if (typeof Permissions !== 'undefined' && typeof Permissions.init === 'function') {
                                Permissions.init(response.user);
                            } else {
                                // Permissions module not loaded yet, will be initialized by auth-interceptor
                                console.log('[Auth.login] Permissions module not loaded yet, will initialize later');
                            }
                        } else {
                            // Fetch user data if not in response
                            this.getCurrentUser().then((userData) => {
                                console.log('[Auth.login] User data loaded:', userData);
                                this.saveUser(userData);
                                // Update avatar immediately if function is available
                                if (typeof updateUserAvatar === 'function') {
                                    updateUserAvatar(userData);
                                }
                                // Initialize permissions
                                if (typeof Permissions !== 'undefined' && typeof Permissions.init === 'function') {
                                    Permissions.init(userData);
                                } else {
                                    console.log('[Auth.login] Permissions module not loaded yet, will initialize later');
                                }
                                resolve(response);
                            }).catch((error) => {
                                console.error('[Auth.login] Failed to fetch user data:', error);
                                resolve(response);
                            });
                            return; // Don't resolve twice
                        }
                        resolve(response);
                    } else {
                        console.error('[Auth.login] No access_token in response:', response);
                        reject(new Error('Invalid response from server'));
                    }
                },
                error: (xhr, status, error) => {
                    const errorMessage = xhr.responseJSON?.message || xhr.responseJSON?.error || 'Login failed';
                    reject(new Error(errorMessage));
                }
            });
        });
    },
    
    /**
     * Logout user
     * @returns {Promise<void>}
     */
    logout: function() {
        return new Promise((resolve, reject) => {
            const token = this.getToken();
            
            if (!token) {
                this.clearTokens();
                resolve();
                return;
            }
            
            $.ajax({
                url: `${this.getApiUrl()}/api/auth/logout`,
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`
                },
                success: () => {
                    this.clearTokens();
                    resolve();
                },
                error: (xhr, status, error) => {
                    // Even if logout fails on server, clear local tokens
                    this.clearTokens();
                    resolve();
                }
            });
        });
    },
    
    /**
     * Refresh access token
     * @returns {Promise<string>}
     */
    refreshToken: function() {
        return new Promise((resolve, reject) => {
            const refreshToken = this.getRefreshToken();
            
            if (!refreshToken) {
                reject(new Error('No refresh token available'));
                return;
            }
            
            $.ajax({
                url: `${this.getApiUrl()}/api/auth/refresh`,
                method: 'POST',
                contentType: 'application/json',
                data: JSON.stringify({
                    refresh_token: refreshToken
                }),
                success: (response) => {
                    if (response.access_token) {
                        this.saveToken(response.access_token, response.refresh_token);
                        resolve(response.access_token);
                    } else {
                        reject(new Error('Invalid response from server'));
                    }
                },
                error: (xhr, status, error) => {
                    // If refresh fails, clear tokens and redirect to login
                    this.clearTokens();
                    reject(new Error('Token refresh failed'));
                }
            });
        });
    },
    
    /**
     * Get current user information
     * @returns {Promise<Object>}
     */
    getCurrentUser: function() {
        return new Promise((resolve, reject) => {
            const token = this.getToken();
            
            if (!token) {
                reject(new Error('No token available'));
                return;
            }
            
            $.ajax({
                url: `${this.getApiUrl()}/api/auth/me`,
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${token}`
                },
                success: (response) => {
                    console.log('[Auth.getCurrentUser] API response:', response);
                    // API returns {success: true, user: {...}}, extract user object
                    let userData = null;
                    if (response.success && response.user) {
                        userData = response.user;
                    } else if (response.user) {
                        // Fallback: if user field exists, use it
                        userData = response.user;
                    } else {
                        // Last resort: return response as-is (for backward compatibility)
                        userData = response;
                    }
                    console.log('[Auth.getCurrentUser] Extracted user data:', userData);
                    console.log('[Auth.getCurrentUser] Username:', userData?.username, 'Roles:', userData?.roles || userData?.role_names || userData?.role_details);
                    resolve(userData);
                },
                error: (xhr, status, error) => {
                    if (xhr.status === 401) {
                        // Token expired, try to refresh
                        this.refreshToken().then(() => {
                            // Retry request
                            return this.getCurrentUser();
                        }).then(resolve).catch(reject);
                    } else {
                        reject(new Error(xhr.responseJSON?.message || 'Failed to get user data'));
                    }
                }
            });
        });
    },
    
    /**
     * Verify token validity
     * @returns {Promise<boolean>}
     */
    verifyToken: function() {
        return new Promise((resolve) => {
            const token = this.getToken();
            
            if (!token) {
                resolve(false);
                return;
            }
            
            this.getCurrentUser().then(() => {
                resolve(true);
            }).catch(() => {
                resolve(false);
            });
        });
    }
};

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = Auth;
}
