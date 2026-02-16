/**
 * Permissions module
 * Manages user permissions and roles on the client side
 * Provides utilities for checking access rights and hiding UI elements
 */

const Permissions = {
    // Cached permissions and roles
    _permissions: null,
    _roles: null,
    _user: null,
    
    /**
     * Initialize permissions from user data
     * @param {Object} userData - User data from API
     */
    init: function(userData) {
        if (!userData) {
            // Try to get from localStorage
            const storedUser = Auth.getUser();
            if (storedUser) {
                userData = storedUser;
            } else {
                console.warn('[Permissions] No user data provided');
                return;
            }
        }
        
        this._user = userData;
        
        // Extract roles
        if (userData.role_details && Array.isArray(userData.role_details)) {
            this._roles = userData.role_details.map(role => role.name);
        } else if (userData.roles) {
            this._roles = Array.isArray(userData.roles) ? userData.roles : [userData.roles];
        } else {
            this._roles = [];
        }
        
        // Extract permissions - API returns permissions directly in user object
        if (userData.permissions && Array.isArray(userData.permissions)) {
            // Permissions are provided directly from API
            this._permissions = userData.permissions.map(perm => typeof perm === 'string' ? perm : perm.name || perm);
        } else if (userData.role_details && Array.isArray(userData.role_details)) {
            // Collect permissions from role_details
            const permissionsSet = new Set();
            userData.role_details.forEach(role => {
                if (role.permissions && Array.isArray(role.permissions)) {
                    role.permissions.forEach(perm => {
                        if (typeof perm === 'string') {
                            permissionsSet.add(perm);
                        } else if (perm.name) {
                            permissionsSet.add(perm.name);
                        }
                    });
                }
            });
            this._permissions = Array.from(permissionsSet);
        } else if (this._roles.length > 0) {
            // If only role names are available, fetch full role details
            this.loadPermissionsFromAPI();
        } else {
            this._permissions = [];
        }
        
        // Store in localStorage for quick access
        localStorage.setItem('user_permissions', JSON.stringify(this._permissions));
        localStorage.setItem('user_roles', JSON.stringify(this._roles));
    },
    
    /**
     * Load permissions from API
     */
    loadPermissionsFromAPI: function() {
        const self = this;
        Auth.getCurrentUser().then(function(userData) {
            self.init(userData);
        }).catch(function(error) {
            console.error('[Permissions] Failed to load permissions from API:', error);
        });
    },
    
    /**
     * Check if user has a specific permission
     * @param {string} permissionName - Permission name (e.g., 'users.create')
     * @returns {boolean}
     */
    hasPermission: function(permissionName) {
        if (!permissionName) return false;
        
        // Admin role has all permissions
        if (this.hasRole('admin')) {
            return true;
        }
        
        // Check cached permissions
        if (this._permissions && this._permissions.includes(permissionName)) {
            return true;
        }
        
        // Try to load from localStorage if not initialized
        if (!this._permissions) {
            const stored = localStorage.getItem('user_permissions');
            if (stored) {
                try {
                    this._permissions = JSON.parse(stored);
                    return this._permissions.includes(permissionName);
                } catch (e) {
                    console.error('[Permissions] Failed to parse stored permissions:', e);
                }
            }
        }
        
        return false;
    },
    
    /**
     * Check if user has a specific role
     * @param {string} roleName - Role name (e.g., 'admin')
     * @returns {boolean}
     */
    hasRole: function(roleName) {
        if (!roleName) return false;
        
        if (this._roles && this._roles.includes(roleName)) {
            return true;
        }
        
        // Try to load from localStorage if not initialized
        if (!this._roles) {
            const stored = localStorage.getItem('user_roles');
            if (stored) {
                try {
                    this._roles = JSON.parse(stored);
                    return this._roles.includes(roleName);
                } catch (e) {
                    console.error('[Permissions] Failed to parse stored roles:', e);
                }
            }
        }
        
        return false;
    },
    
    /**
     * Check if user has any of the specified roles
     * @param {string[]} roleNames - Array of role names
     * @returns {boolean}
     */
    hasAnyRole: function(roleNames) {
        if (!Array.isArray(roleNames) || roleNames.length === 0) return false;
        return roleNames.some(roleName => this.hasRole(roleName));
    },
    
    /**
     * Check if user has all of the specified roles
     * @param {string[]} roleNames - Array of role names
     * @returns {boolean}
     */
    hasAllRoles: function(roleNames) {
        if (!Array.isArray(roleNames) || roleNames.length === 0) return false;
        return roleNames.every(roleName => this.hasRole(roleName));
    },
    
    /**
     * Get all user permissions
     * @returns {string[]}
     */
    getPermissions: function() {
        if (this._permissions) {
            return [...this._permissions];
        }
        
        // Try to load from localStorage
        const stored = localStorage.getItem('user_permissions');
        if (stored) {
            try {
                return JSON.parse(stored);
            } catch (e) {
                console.error('[Permissions] Failed to parse stored permissions:', e);
            }
        }
        
        return [];
    },
    
    /**
     * Get all user roles
     * @returns {string[]}
     */
    getRoles: function() {
        if (this._roles) {
            return [...this._roles];
        }
        
        // Try to load from localStorage
        const stored = localStorage.getItem('user_roles');
        if (stored) {
            try {
                return JSON.parse(stored);
            } catch (e) {
                console.error('[Permissions] Failed to parse stored roles:', e);
            }
        }
        
        return [];
    },
    
    /**
     * Group permissions by resource
     * @param {Array} permissions - Array of permission objects or names
     * @returns {Object} - Grouped permissions { resource: [permissions] }
     */
    groupByResource: function(permissions) {
        const grouped = {};
        
        permissions.forEach(perm => {
            const permName = typeof perm === 'string' ? perm : perm.name;
            if (!permName) return;
            
            const parts = permName.split('.');
            if (parts.length >= 2) {
                const resource = parts[0];
                if (!grouped[resource]) {
                    grouped[resource] = [];
                }
                grouped[resource].push(perm);
            }
        });
        
        return grouped;
    },
    
    /**
     * Apply permission-based visibility to elements
     * Hides elements with data-require-permission attribute if user doesn't have the permission
     */
    applyVisibility: function() {
        const self = this;
        
        // Hide elements based on permissions
        document.querySelectorAll('[data-require-permission]').forEach(function(element) {
            const requiredPermission = element.getAttribute('data-require-permission');
            if (requiredPermission && !self.hasPermission(requiredPermission)) {
                element.style.display = 'none';
            } else {
                element.style.display = '';
            }
        });
        
        // Hide elements based on roles
        document.querySelectorAll('[data-require-role]').forEach(function(element) {
            const requiredRole = element.getAttribute('data-require-role');
            if (requiredRole && !self.hasRole(requiredRole)) {
                element.style.display = 'none';
            } else {
                element.style.display = '';
            }
        });
        
        // Hide elements based on any role
        document.querySelectorAll('[data-require-any-role]').forEach(function(element) {
            const requiredRoles = element.getAttribute('data-require-any-role');
            if (requiredRoles) {
                const roles = requiredRoles.split(',').map(r => r.trim());
                if (!self.hasAnyRole(roles)) {
                    element.style.display = 'none';
                } else {
                    element.style.display = '';
                }
            }
        });
    },
    
    /**
     * Clear cached permissions
     */
    clear: function() {
        this._permissions = null;
        this._roles = null;
        this._user = null;
        localStorage.removeItem('user_permissions');
        localStorage.removeItem('user_roles');
    }
};

// Initialize permissions when page loads (if user data is available)
if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', function() {
        // Wait a bit for auth-interceptor to load user data
        setTimeout(function() {
            const userData = Auth.getUser();
            if (userData) {
                Permissions.init(userData);
                Permissions.applyVisibility();
            }
        }, 500);
    });
}

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = Permissions;
}
