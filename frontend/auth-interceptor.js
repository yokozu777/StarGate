/**
 * Global jQuery AJAX interceptor
 * Automatically adds Authorization header to all AJAX requests
 * Handles 401 errors and redirects to login
 */

(function() {
    'use strict';

    // Wait for jQuery to be available - use DOMContentLoaded or wait
    function initAuthInterceptor() {
        if (typeof jQuery === 'undefined' || typeof $ === 'undefined') {
            console.warn('jQuery not yet available, waiting...');
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', initAuthInterceptor);
            } else {
                setTimeout(initAuthInterceptor, 100);
            }
            return;
        }

        console.log('[AuthInterceptor] jQuery available, initializing...');

        // Setup AJAX interceptor
        $(document).ajaxSend(function(event, xhr, settings) {
            // Skip auth endpoints (login, refresh)
            if (settings.url && (
                settings.url.includes('/api/auth/login') ||
                settings.url.includes('/api/auth/refresh')
            )) {
                return;
            }

            // Add Authorization header if token exists
            const token = Auth.getToken();
            if (token) {
                xhr.setRequestHeader('Authorization', `Bearer ${token}`);
                console.log('[AuthInterceptor] Token added to request:', settings.url);
            } else {
                console.warn('[AuthInterceptor] No token available for request:', settings.url);
            }
        });

        // Handle 401 errors globally
        $(document).ajaxError(function(event, xhr, settings) {
            // Skip auth endpoints
            if (settings.url && (
                settings.url.includes('/api/auth/login') ||
                settings.url.includes('/api/auth/refresh') ||
                settings.url.includes('/api/auth/logout')
            )) {
                return;
            }

            // Handle 401 Unauthorized
            if (xhr.status === 401) {
                console.warn('Unauthorized request detected, redirecting to login');
                
                // Clear tokens
                Auth.clearTokens();
                
                // Redirect to login page
                if (window.location.pathname !== '/login.html' && 
                    !window.location.pathname.endsWith('login.html')) {
                    window.location.href = 'login.html';
                }
            }
        });

        // Check authentication on page load
        $(document).ready(function() {
            // Skip check on login page
            if (window.location.pathname.includes('login.html')) {
                return;
            }

        // Verify token
        const token = Auth.getToken();
        console.log('[AuthInterceptor] Checking authentication, token exists:', !!token);
        
        if (!token) {
            console.warn('[AuthInterceptor] No token found, redirecting to login');
            window.location.href = 'login.html';
            return;
        }
        
        Auth.verifyToken().then(function(isValid) {
            console.log('[AuthInterceptor] Token verification result:', isValid);
            if (!isValid) {
                console.warn('[AuthInterceptor] Token invalid or expired, redirecting to login');
                Auth.clearTokens();
                window.location.href = 'login.html';
            } else {
                // Always reload user data to ensure it's fresh and complete
                // Wait a bit for sidebar to render first
                setTimeout(function() {
                    console.log('[AuthInterceptor] Starting to load user data...');
                    Auth.getCurrentUser().then(function(userData) {
                        console.log('[AuthInterceptor] User data loaded from API:', userData);
                        if (userData && userData.username) {
                            Auth.saveUser(userData);
                            console.log('[AuthInterceptor] User data saved, username:', userData.username, 'roles:', userData.roles || userData.role_names || userData.role_details);
                            
                            // Force sidebar re-render if it exists to update user info
                            if (typeof renderSidebar === 'function') {
                                console.log('[AuthInterceptor] Re-rendering sidebar with fresh user data');
                                renderSidebar();
                            }
                            
                            // Render top header avatar
                            if (typeof renderTopHeaderAvatar === 'function') {
                                console.log('[AuthInterceptor] Rendering top header avatar');
                                renderTopHeaderAvatar();
                            }
                            
                            // Update avatar with retry mechanism
                            updateUserAvatar(userData);
                            // Also try again after delays to ensure DOM is ready
                            setTimeout(function() {
                                updateUserAvatar(userData);
                            }, 500);
                            setTimeout(function() {
                                updateUserAvatar(userData);
                            }, 1000);
                            setTimeout(function() {
                                updateUserAvatar(userData);
                            }, 2000);
                            // Initialize permissions
                            if (typeof Permissions !== 'undefined' && typeof Permissions.init === 'function') {
                                Permissions.init(userData);
                                Permissions.applyVisibility();
                            } else {
                                console.warn('[AuthInterceptor] Permissions module not available');
                            }
                        } else {
                            console.warn('[AuthInterceptor] No user data or username returned from API:', userData);
                            // Fallback to cached data
                            const cachedUser = Auth.getUser();
                            if (cachedUser) {
                                console.log('[AuthInterceptor] Using cached user data as fallback:', cachedUser);
                                if (typeof renderTopHeaderAvatar === 'function') {
                                    renderTopHeaderAvatar();
                                }
                                updateUserAvatar(cachedUser);
                                setTimeout(function() {
                                    updateUserAvatar(cachedUser);
                                }, 500);
                            }
                        }
                    }).catch(function(error) {
                        console.error('[AuthInterceptor] Failed to load user data:', error);
                        // Fallback to cached data
                        const cachedUser = Auth.getUser();
                        if (cachedUser) {
                            console.log('[AuthInterceptor] Using cached user data after error:', cachedUser);
                            if (typeof renderTopHeaderAvatar === 'function') {
                                renderTopHeaderAvatar();
                            }
                            updateUserAvatar(cachedUser);
                            setTimeout(function() {
                                updateUserAvatar(cachedUser);
                            }, 500);
                            if (typeof Permissions !== 'undefined' && typeof Permissions.init === 'function') {
                                Permissions.init(cachedUser);
                                Permissions.applyVisibility();
                            }
                        }
                    });
                }, 300);
            }
        }).catch(function(error) {
            console.error('Token verification failed:', error);
            Auth.clearTokens();
            window.location.href = 'login.html';
        });
    });

    /**
     * Update user avatar in navbar
     */
    function updateUserAvatar(userData) {
        console.log('[updateUserAvatar] Updating avatar with data:', userData);
        console.log('[updateUserAvatar] UserData structure:', {
            username: userData?.username,
            roles: userData?.roles,
            role_details: userData?.role_details,
            permissions: userData?.permissions
        });
        
        // Wait for DOM to be ready
        let retryCount = 0;
        const maxRetries = 10;
        
        const tryUpdate = () => {
            const avatarContainer = $('#user-avatar-container');
            console.log('[updateUserAvatar] Attempt', retryCount + 1, '- Avatar container found:', avatarContainer.length > 0);
            
            if (avatarContainer.length && userData) {
                const username = userData.username || 'User';
                const initials = username.substring(0, 2).toUpperCase();
                console.log('[updateUserAvatar] Setting username to:', username, 'initials:', initials);
                
                // Update top header avatar (new location)
                const avatarTextTop = avatarContainer.find('.user-avatar-text-top');
                if (avatarTextTop.length) {
                    avatarTextTop.text(initials);
                    console.log('[updateUserAvatar] Top header avatar text updated to:', initials);
                }
                
                // Update dropdown username in top header
                const dropdownUsernameTop = $('#user-dropdown-username-top');
                if (dropdownUsernameTop.length) {
                    dropdownUsernameTop.text(username);
                    console.log('[updateUserAvatar] Top header username updated to:', username);
                }
                
                // Update dropdown email in top header (instead of roles)
                const dropdownEmailTop = $('#user-dropdown-email-top');
                if (dropdownEmailTop.length) {
                    const email = userData.email || '';
                    dropdownEmailTop.text(email || 'No email');
                    console.log('[updateUserAvatar] Top header email updated to:', email || 'No email');
                }
                
                // Legacy: Update sidebar avatar if it exists
                const avatarText = avatarContainer.find('.user-avatar-text');
                if (avatarText.length) {
                    avatarText.text(initials);
                    console.log('[updateUserAvatar] Sidebar avatar text updated to:', initials);
                }
                
                const dropdownUsername = $('#user-dropdown-username');
                if (dropdownUsername.length) {
                    dropdownUsername.text(username);
                    console.log('[updateUserAvatar] Sidebar username updated to:', username);
                }
                
                const dropdownRoles = $('#user-dropdown-roles');
                if (dropdownRoles.length) {
                    let rolesToDisplay = null;
                    if (userData.role_names && userData.role_names.length > 0) {
                        rolesToDisplay = userData.role_names.join(', ');
                    } else if (userData.role_details && userData.role_details.length > 0) {
                        rolesToDisplay = userData.role_details.map(r => typeof r === 'string' ? r : (r.name || r)).join(', ');
                    } else if (userData.roles && userData.roles.length > 0) {
                        rolesToDisplay = Array.isArray(userData.roles) 
                            ? userData.roles.join(', ') 
                            : userData.roles;
                    }
                    
                    if (rolesToDisplay) {
                        dropdownRoles.text(rolesToDisplay);
                    } else {
                        dropdownRoles.text('No roles');
                    }
                }
            } else {
                if (!avatarContainer.length && retryCount < maxRetries) {
                    retryCount++;
                    console.warn('[updateUserAvatar] Avatar container not found, retrying... (' + retryCount + '/' + maxRetries + ')');
                    setTimeout(tryUpdate, 200);
                } else if (!avatarContainer.length) {
                    console.error('[updateUserAvatar] Avatar container not found after', maxRetries, 'retries');
                } else {
                    console.warn('[updateUserAvatar] No user data provided');
                }
            }
        };
        
        // Try immediately, and also after DOM is ready
        if (document.readyState === 'loading') {
            $(document).ready(tryUpdate);
        } else {
            tryUpdate();
        }
    }

        // Expose updateUserAvatar for external use
        if (typeof window !== 'undefined') {
            window.updateUserAvatar = updateUserAvatar;
        }
    }

    // Start initialization
    initAuthInterceptor();
})();
