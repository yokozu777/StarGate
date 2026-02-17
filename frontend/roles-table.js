/**
 * RolesTable component
 * Displays and manages roles table
 */

const RolesTable = {
    roles: [],
    loading: false,
    
    /**
     * Initialize roles table
     */
    init: function() {
        this.loadRoles();
    },
    
    /**
     * Load roles from API
     */
    loadRoles: function() {
        const self = this;
        this.loading = true;
        this.updateLoadingState();
        
        // Try using apiRequest if available
        if (typeof apiRequest === 'function') {
            apiRequest('/api/roles', {}, {
                context: 'Load Roles',
                notify: false
            }).then(function(response) {
                if (response.success && response.roles) {
                    self.roles = response.roles;
                } else {
                    self.roles = [];
                }
                self.loading = false;
                self.render();
                self.updateLoadingState();
            }).catch(function(error) {
                console.error('[RolesTable] Failed to load roles:', error);
                self.showError('Failed to load roles: ' + (error.message || 'Unknown error'));
                self.roles = [];
                self.loading = false;
                self.render();
                self.updateLoadingState();
            });
            return;
        }
        
        // Fallback to $.ajax
        const token = Auth.getToken();
        if (!token) {
            console.error('[RolesTable] No token available');
            self.showError('Authentication required. Please login again.');
            self.loading = false;
            self.render();
            return;
        }
        
        $.ajax({
            url: `${Auth.getApiUrl()}/api/roles`,
            method: 'GET',
            headers: {
                'Authorization': `Bearer ${token}`
            },
            timeout: 30000,
            success: function(response) {
                if (response.success && response.roles) {
                    self.roles = response.roles;
                } else {
                    self.roles = [];
                }
                self.loading = false;
                self.render();
                self.updateLoadingState();
            },
            error: function(xhr, status, error) {
                console.error('[RolesTable] Failed to load roles:', error);
                self.showError('Failed to load roles: ' + (xhr.responseJSON?.error || error));
                self.roles = [];
                self.loading = false;
                self.render();
                self.updateLoadingState();
            }
        });
    },
    
    /**
     * Render roles table
     */
    render: function() {
        const container = document.getElementById('roles-table-container');
        if (!container) {
            console.warn('[RolesTable] Container roles-table-container not found, retrying in 100ms...');
            // Retry after a short delay if container doesn't exist yet
            setTimeout(() => {
                const retryContainer = document.getElementById('roles-table-container');
                if (retryContainer) {
                    console.log('[RolesTable] Container found on retry, rendering...');
                    this.render();
                } else {
                    console.error('[RolesTable] Container still not found after retry');
                }
            }, 100);
            return;
        }
        
        if (this.loading) {
            container.innerHTML = `
                <div style="text-align: center; padding: 40px; color: var(--text-secondary);">
                    <i class="fas fa-spinner fa-spin" style="font-size: 24px; margin-bottom: 12px; display: block;"></i>
                    <div>Loading roles...</div>
                </div>
            `;
            return;
        }
        
        if (this.roles.length === 0) {
            container.innerHTML = `
                <div style="text-align: center; padding: 40px; color: var(--text-secondary);">
                    <i class="fas fa-user-shield" style="font-size: 32px; margin-bottom: 12px; display: block; opacity: 0.5;"></i>
                    <div>No roles found</div>
                </div>
            `;
            return;
        }
        
        const canCreate = Permissions.hasPermission('roles.create');
        const canEdit = Permissions.hasPermission('roles.update');
        const canDelete = Permissions.hasPermission('roles.delete');
        
        let html = `
            <div style="margin-bottom: 16px;">
                ${canCreate ? `
                    <button onclick="RolesTable.openEditor()" 
                            class="btn btn-primary"
                            style="padding: 10px 20px; background: var(--accent-primary); color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500;">
                        <i class="fas fa-plus" style="margin-right: 8px;"></i>
                        Create Role
                    </button>
                ` : ''}
            </div>
            
            <div style="background: var(--bg-surface); border: 1px solid var(--border-muted); border-radius: var(--border-radius); overflow: hidden;">
                <table style="width: 100%; border-collapse: collapse;">
                    <thead>
                        <tr style="background: var(--bg-elevated); border-bottom: 1px solid var(--border-muted);">
                            <th style="padding: 12px 16px; text-align: left; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Name</th>
                            <th style="padding: 12px 16px; text-align: left; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Description</th>
                            <th style="padding: 12px 16px; text-align: left; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Permissions</th>
                            <th style="padding: 12px 16px; text-align: left; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Created At</th>
                            <th style="padding: 12px 16px; text-align: right; color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${this.roles.map(role => {
                            const permissionsCount = role.permissions ? (Array.isArray(role.permissions) ? role.permissions.length : 0) : 0;
                            return `
                            <tr style="border-bottom: 1px solid var(--border-muted);">
                                <td style="padding: 12px 16px; color: var(--text-primary); font-size: 14px; font-weight: 500;">${this.escapeHtml(role.name || '')}</td>
                                <td style="padding: 12px 16px; color: var(--text-secondary); font-size: 14px;">${this.escapeHtml(role.description || '-')}</td>
                                <td style="padding: 12px 16px;">
                                    <span style="display: inline-block; padding: 4px 8px; background: var(--badge-purple-bg); border: 1px solid var(--badge-purple-border); border-radius: 4px; color: var(--badge-purple-text); font-size: 12px;">
                                        ${permissionsCount} permission${permissionsCount !== 1 ? 's' : ''}
                                    </span>
                                </td>
                                <td style="padding: 12px 16px; color: var(--text-secondary); font-size: 13px;">
                                    ${role.created_at ? new Date(role.created_at).toLocaleDateString() : '-'}
                                </td>
                                <td style="padding: 12px 16px; text-align: right;">
                                    <div style="display: flex; gap: 8px; justify-content: flex-end;">
                                        ${canEdit ? `
                                            <button onclick="RolesTable.openEditor('${role.id}')" 
                                                    style="padding: 6px 12px; background: transparent; border: 1px solid var(--border-muted); border-radius: 4px; color: var(--text-primary); cursor: pointer; font-size: 12px;">
                                                <i class="fas fa-edit"></i>
                                            </button>
                                        ` : ''}
                                        ${canDelete ? `
                                            <button onclick="RolesTable.deleteRole('${role.id}', '${this.escapeHtml(role.name)}')" 
                                                    style="padding: 6px 12px; background: transparent; border: 1px solid var(--border-muted); border-radius: 4px; color: var(--text-danger, #ef4444); cursor: pointer; font-size: 12px;">
                                                <i class="fas fa-trash"></i>
                                            </button>
                                        ` : ''}
                                    </div>
                                </td>
                            </tr>
                        `;
                        }).join('')}
                    </tbody>
                </table>
            </div>
        `;
        
        container.innerHTML = html;
    },
    
    /**
     * Open role editor modal
     */
    openEditor: function(roleId) {
        if (typeof RoleEditor !== 'undefined') {
            RoleEditor.open(roleId);
        } else {
            console.error('[RolesTable] RoleEditor not found');
        }
    },
    
    /**
     * Delete role
     */
    deleteRole: function(roleId, roleName) {
        if (!confirm(`Are you sure you want to delete role "${roleName}"?`)) {
            return;
        }
        
        const self = this;
        $.ajax({
            url: `${Auth.getApiUrl()}/api/roles/${roleId}`,
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${Auth.getToken()}`
            },
            success: function(response) {
                if (response.success) {
                    self.showSuccess('Role deleted successfully');
                    self.loadRoles();
                } else {
                    self.showError(response.error || 'Failed to delete role');
                }
            },
            error: function(xhr, status, error) {
                self.showError('Failed to delete role: ' + (xhr.responseJSON?.error || error));
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
