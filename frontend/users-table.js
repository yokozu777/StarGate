/**
 * UsersTable component
 * Displays and manages users table
 */

const UsersTable = {
    users: [],
    loading: false,
    
    /**
     * Initialize users table
     */
    init: function() {
        this.loadUsers();
    },
    
    /**
     * Load users from API
     */
    loadUsers: function() {
        const self = this;
        this.loading = true;
        this.updateLoadingState();
        
        const token = Auth.getToken();
        if (!token) {
            console.error('[UsersTable] No token available');
            self.showError('Authentication required. Please login again.');
            self.loading = false;
            self.render();
            return;
        }
        
        console.log('[UsersTable] Loading users from:', `${Auth.getApiUrl()}/api/users`);
        console.log('[UsersTable] Token available:', !!token);
        
        // Try using apiRequest if available (it handles auth automatically)
        if (typeof apiRequest === 'function') {
            console.log('[UsersTable] Using apiRequest function');
            apiRequest('/api/users', {}, {
                context: 'Load Users',
                notify: false
            }).then(function(response) {
                console.log('[UsersTable] Response received via apiRequest:', response);
                if (response.success && response.users) {
                    self.users = response.users;
                    console.log('[UsersTable] Users loaded:', self.users);
                    // Log first user's roles structure for debugging
                    if (self.users.length > 0) {
                        console.log('[UsersTable] First user roles structure:', {
                            roles: self.users[0].roles,
                            role_names: self.users[0].role_names,
                            role_details: self.users[0].role_details
                        });
                    }
                } else {
                    self.users = [];
                }
                self.loading = false;
                self.render();
                self.updateLoadingState();
            }).catch(function(error) {
                console.error('[UsersTable] Error via apiRequest:', error);
                self.showError('Failed to load users: ' + (error.message || 'Unknown error'));
                self.users = [];
                self.render();
                self.loading = false;
                self.updateLoadingState();
            });
            return;
        }
        
        // Fallback to $.ajax
        const apiUrl = `${Auth.getApiUrl()}/api/users`;
        console.log('[UsersTable] Using $.ajax, making request to:', apiUrl);
        
        $.ajax({
            url: apiUrl,
            method: 'GET',
            headers: {
                'Authorization': `Bearer ${token}`
            },
            timeout: 30000, // 30 seconds timeout
            beforeSend: function(xhr) {
                console.log('[UsersTable] Request starting...');
            },
            success: function(response) {
                console.log('[UsersTable] Response received:', response);
                if (response.success && response.users) {
                    self.users = response.users;
                    console.log('[UsersTable] Users loaded:', self.users);
                    // Log first user's roles structure for debugging
                    if (self.users.length > 0) {
                        console.log('[UsersTable] First user roles structure:', {
                            roles: self.users[0].roles,
                            role_names: self.users[0].role_names,
                            role_details: self.users[0].role_details
                        });
                    }
                } else {
                    self.users = [];
                }
                self.loading = false;
                self.render();
                self.updateLoadingState();
            },
            error: function(xhr, status, error) {
                console.error('[UsersTable] Failed to load users:', {
                    status: xhr.status,
                    statusText: xhr.statusText,
                    error: error,
                    response: xhr.responseJSON,
                    url: xhr.responseURL || `${Auth.getApiUrl()}/api/users`
                });
                
                let errorMessage = 'Failed to load users';
                if (xhr.status === 401) {
                    errorMessage = 'Authentication required. Please login again.';
                    // Redirect to login
                    setTimeout(() => {
                        window.location.href = 'login.html';
                    }, 2000);
                } else if (xhr.status === 403) {
                    errorMessage = 'Access denied. You don\'t have permission to view users.';
                } else if (xhr.status === 0 || status === 'timeout') {
                    errorMessage = 'Request timeout. Please check your connection.';
                } else if (xhr.responseJSON && xhr.responseJSON.error) {
                    errorMessage = xhr.responseJSON.error;
                } else {
                    errorMessage = error || 'Unknown error';
                }
                
                self.showError(errorMessage);
                self.users = [];
                self.render();
                self.loading = false;
                self.updateLoadingState();
            }
        });
    },
    
    /**
     * Render users table
     */
    render: function() {
        const container = document.getElementById('users-table-container');
        if (!container) {
            console.warn('[UsersTable] Container users-table-container not found, retrying in 100ms...');
            // Retry after a short delay if container doesn't exist yet
            setTimeout(() => {
                const retryContainer = document.getElementById('users-table-container');
                if (retryContainer) {
                    console.log('[UsersTable] Container found on retry, rendering...');
                    this.render();
                } else {
                    console.error('[UsersTable] Container still not found after retry');
                }
            }, 100);
            return;
        }
        
        if (this.loading) {
            container.innerHTML = `
                <div style="text-align: center; padding: 40px; color: var(--text-secondary);">
                    <i class="fas fa-spinner fa-spin" style="font-size: 24px; margin-bottom: 12px; display: block;"></i>
                    <div>Loading users...</div>
                </div>
            `;
            return;
        }
        
        if (this.users.length === 0) {
            container.innerHTML = `
                <div style="text-align: center; padding: 40px; color: var(--text-secondary);">
                    <i class="fas fa-users" style="font-size: 32px; margin-bottom: 12px; display: block; opacity: 0.5;"></i>
                    <div>No users found</div>
                </div>
            `;
            return;
        }
        
        const canCreate = Permissions.hasPermission('users.create');
        const canEdit = Permissions.hasPermission('users.update');
        const canDelete = Permissions.hasPermission('users.delete');
        
        let html = `
            <div style="margin-bottom: 16px;">
                ${canCreate ? `
                    <button onclick="UsersTable.openEditor()" 
                            class="btn btn-primary"
                            style="padding: 10px 20px; background: var(--accent-primary); color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500;">
                        <i class="fas fa-plus" style="margin-right: 8px;"></i>
                        Create User
                    </button>
                ` : ''}
            </div>
            
            <div style="background: var(--bg-surface); border: 1px solid var(--border-muted); border-radius: var(--border-radius); overflow: hidden;">
                <table style="width: 100%; border-collapse: collapse;">
                    <thead>
                        <tr style="background: var(--bg-elevated); border-bottom: 1px solid var(--border-muted);">
                            <th style="padding: 12px 16px; text-align: left; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Username</th>
                            <th style="padding: 12px 16px; text-align: left; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Email</th>
                            <th style="padding: 12px 16px; text-align: left; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Roles</th>
                            <th style="padding: 12px 16px; text-align: left; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Status</th>
                            <th style="padding: 12px 16px; text-align: left; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Created At</th>
                            <th style="padding: 12px 16px; text-align: right; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${this.users.map(user => `
                            <tr style="border-bottom: 1px solid var(--border-muted);">
                                <td style="padding: 12px 16px; color: var(--text-primary); font-size: 14px;">${this.escapeHtml(user.username || '')}</td>
                                <td style="padding: 12px 16px; color: var(--text-secondary); font-size: 14px;">${this.escapeHtml(user.email || '-')}</td>
                                <td style="padding: 12px 16px;">
                                    ${(() => {
                                        // Use role_names if available (from API), otherwise try role_details, fallback to roles
                                        let rolesToDisplay = [];
                                        if (user.role_names && user.role_names.length > 0) {
                                            rolesToDisplay = user.role_names;
                                        } else if (user.role_details && user.role_details.length > 0) {
                                            rolesToDisplay = user.role_details.map(r => typeof r === 'string' ? r : (r.name || r));
                                        } else if (user.roles && user.roles.length > 0) {
                                            // If only IDs are available, we can't display them properly
                                            // This shouldn't happen if API works correctly
                                            rolesToDisplay = [];
                                        }
                                        
                                        if (rolesToDisplay.length > 0) {
                                            return rolesToDisplay.map(role => `
                                                <span style="display: inline-block; padding: 4px 8px; background: var(--badge-blue-bg); border: 1px solid var(--badge-blue-border); border-radius: 4px; color: var(--badge-blue-text); font-size: 12px; margin-right: 4px; margin-bottom: 4px;">
                                                    ${this.escapeHtml(role)}
                                                </span>
                                            `).join('');
                                        } else {
                                            return '<span style="color: var(--text-muted); font-size: 12px;">No roles</span>';
                                        }
                                    })()}
                                </td>
                                <td style="padding: 12px 16px;">
                                    ${(() => {
                                        const active = user.is_active !== false;
                                        const currentUser = Auth.getUser();
                                        const currentUserId = currentUser?.user_id || currentUser?.id;
                                        const isSelf = user.id === currentUserId;
                                        if (isSelf) {
                                            return `<span style="padding: 4px 8px; border-radius: 4px; font-size: 12px; background: var(--badge-blue-bg); color: var(--badge-blue-text);">Active</span>`;
                                        }
                                        if (!canEdit) return active ? 'Active' : 'Disabled';
                                        return active
                                            ? `<button onclick="UsersTable.toggleUserActive('${user.id}', true)" style="padding: 6px 12px; background: transparent; border: 1px solid var(--border-muted); border-radius: 4px; color: var(--text-warning, #eab308); cursor: pointer; font-size: 12px;" title="Disable user"><i class="fas fa-user-slash" style="margin-right: 4px;"></i>Disable</button>`
                                            : `<button onclick="UsersTable.toggleUserActive('${user.id}', false)" style="padding: 6px 12px; background: transparent; border: 1px solid var(--border-muted); border-radius: 4px; color: var(--text-success, #22c55e); cursor: pointer; font-size: 12px;" title="Enable user"><i class="fas fa-user-check" style="margin-right: 4px;"></i>Enable</button>`;
                                    })()}
                                </td>
                                <td style="padding: 12px 16px; color: var(--text-secondary); font-size: 13px;">
                                    ${user.created_at ? new Date(user.created_at).toLocaleDateString() : '-'}
                                </td>
                                <td style="padding: 12px 16px; text-align: right;">
                                    <div style="display: flex; gap: 8px; justify-content: flex-end;">
                                        ${canEdit ? `
                                            <button onclick="UsersTable.openEditor('${user.id}')" 
                                                    style="padding: 6px 12px; background: transparent; border: 1px solid var(--border-muted); border-radius: 4px; color: var(--text-primary); cursor: pointer; font-size: 12px;">
                                                <i class="fas fa-edit"></i>
                                            </button>
                                        ` : ''}
                                        ${canDelete ? `
                                            <button onclick="UsersTable.deleteUser('${user.id}', '${this.escapeHtml(user.username)}')" 
                                                    style="padding: 6px 12px; background: transparent; border: 1px solid var(--border-muted); border-radius: 4px; color: var(--text-danger, #ef4444); cursor: pointer; font-size: 12px;">
                                                <i class="fas fa-trash"></i>
                                            </button>
                                        ` : ''}
                                    </div>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        `;
        
        container.innerHTML = html;
    },
    
    /**
     * Open user editor modal
     */
    openEditor: function(userId) {
        if (typeof UserEditor !== 'undefined') {
            UserEditor.open(userId);
        } else {
            console.error('[UsersTable] UserEditor not found');
        }
    },
    
    /**
     * Toggle user active status (Disable/Enable)
     */
    toggleUserActive: function(userId, currentlyActive) {
        const newActive = !currentlyActive;
        const self = this;
        $.ajax({
            url: `${Auth.getApiUrl()}/api/users/${userId}`,
            method: 'PUT',
            contentType: 'application/json',
            headers: {
                'Authorization': `Bearer ${Auth.getToken()}`
            },
            data: JSON.stringify({ is_active: newActive }),
            success: function(response) {
                if (response.success) {
                    self.showSuccess(newActive ? 'User enabled' : 'User disabled');
                    self.loadUsers();
                } else {
                    self.showError(response.error || 'Failed to update user status');
                }
            },
            error: function(xhr, status, error) {
                self.showError('Failed to update user status: ' + (xhr.responseJSON?.error || error));
            }
        });
    },

    /**
     * Delete user
     */
    deleteUser: function(userId, username) {
        if (!confirm(`Are you sure you want to delete user "${username}"?`)) {
            return;
        }
        
        const self = this;
        $.ajax({
            url: `${Auth.getApiUrl()}/api/users/${userId}`,
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${Auth.getToken()}`
            },
            success: function(response) {
                if (response.success) {
                    self.showSuccess('User deleted successfully');
                    self.loadUsers();
                } else {
                    self.showError(response.error || 'Failed to delete user');
                }
            },
            error: function(xhr, status, error) {
                self.showError('Failed to delete user: ' + (xhr.responseJSON?.error || error));
            }
        });
    },
    
    /**
     * Update loading state
     */
    updateLoadingState: function() {
        // Can be used to update UI indicators
    },
    
    /**
     * Show success message
     */
    showSuccess: function(message) {
        // Use toast notification if available
        if (typeof Toast !== 'undefined' && Toast.success) {
            Toast.success(message);
        } else {
            alert(message);
        }
    },
    
    /**
     * Show error message
     */
    showError: function(message) {
        // Use toast notification if available
        if (typeof Toast !== 'undefined' && Toast.error) {
            Toast.error(message);
        } else {
            alert(message);
        }
    },
    
    /**
     * Escape HTML
     */
    escapeHtml: function(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
};
