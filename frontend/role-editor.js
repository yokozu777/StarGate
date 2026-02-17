/**
 * RoleEditor component
 * Modal for creating and editing roles with permissions selection
 */

const RoleEditor = {
    currentRoleId: null,
    availablePermissions: [],
    
    /**
     * Open editor modal
     */
    open: function(roleId) {
        this.currentRoleId = roleId;
        this.loadPermissions().then(() => {
            this.render();
            this.showModal();
        });
    },
    
    /**
     * Load available permissions from API
     */
    loadPermissions: function() {
        const self = this;
        return new Promise((resolve, reject) => {
            $.ajax({
                url: `${Auth.getApiUrl()}/api/permissions`,
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${Auth.getToken()}`
                },
                success: function(response) {
                    if (response.success && response.permissions) {
                        self.availablePermissions = response.permissions;
                    } else {
                        self.availablePermissions = [];
                    }
                    resolve();
                },
                error: function(xhr, status, error) {
                    console.error('[RoleEditor] Failed to load permissions:', error);
                    self.availablePermissions = [];
                    resolve(); // Continue even if permissions fail to load
                }
            });
        });
    },
    
    /**
     * Load role data if editing
     */
    loadRole: function() {
        if (!this.currentRoleId) {
            return Promise.resolve(null);
        }
        
        const self = this;
        return new Promise((resolve, reject) => {
            $.ajax({
                url: `${Auth.getApiUrl()}/api/roles/${this.currentRoleId}`,
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${Auth.getToken()}`
                },
                success: function(response) {
                    if (response.success && response.role) {
                        resolve(response.role);
                    } else {
                        reject(new Error('Role not found'));
                    }
                },
                error: function(xhr, status, error) {
                    reject(new Error(xhr.responseJSON?.error || error));
                }
            });
        });
    },
    
    /**
     * Group permissions by resource
     */
    groupPermissionsByResource: function() {
        const grouped = {};
        
        this.availablePermissions.forEach(perm => {
            const resource = perm.resource || (perm.name ? perm.name.split('.')[0] : 'other');
            if (!grouped[resource]) {
                grouped[resource] = [];
            }
            grouped[resource].push(perm);
        });
        
        return grouped;
    },
    
    /**
     * Render modal
     */
    render: function() {
        const modalId = 'role-editor-modal';
        let modal = document.getElementById(modalId);
        
        if (!modal) {
            modal = document.createElement('div');
            modal.id = modalId;
            modal.className = 'modal';
            modal.style.cssText = 'display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.5);';
            document.body.appendChild(modal);
        }
        
        const isEdit = !!this.currentRoleId;
        const title = isEdit ? 'Edit Role' : 'Create Role';
        const groupedPermissions = this.groupPermissionsByResource();
        
        modal.innerHTML = `
            <div style="position: relative; background: var(--bg-surface); border: 1px solid var(--border-muted); border-radius: var(--border-radius); max-width: 800px; margin: 30px auto; padding: 0; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.3); max-height: 90vh; display: flex; flex-direction: column;">
                <div style="padding: 20px 24px; border-bottom: 1px solid var(--border-muted); display: flex; justify-content: space-between; align-items: center; flex-shrink: 0;">
                    <h2 style="margin: 0; color: var(--text-primary); font-size: 18px; font-weight: 600;">${title}</h2>
                    <button onclick="RoleEditor.close()" style="background: transparent; border: none; color: var(--text-secondary); cursor: pointer; font-size: 20px; padding: 0; width: 32px; height: 32px; display: flex; align-items: center; justify-content: center;">
                        <i class="fas fa-times"></i>
                    </button>
                </div>
                
                <div style="padding: 24px; overflow-y: auto; flex: 1;">
                    <form id="role-editor-form" onsubmit="RoleEditor.save(event)">
                        <div style="margin-bottom: 20px;">
                            <label style="display: block; color: var(--text-primary); font-size: 14px; font-weight: 500; margin-bottom: 8px;">
                                Name <span style="color: var(--text-danger, #ef4444);">*</span>
                            </label>
                            <input type="text" 
                                   id="role-name" 
                                   required
                                   minlength="2"
                                   maxlength="100"
                                   style="width: 100%; padding: 10px 12px; background: var(--bg-elevated); border: 1px solid var(--border-muted); border-radius: 6px; color: var(--text-primary); font-size: 14px;"
                                   placeholder="Enter role name">
                            <div id="role-name-error" style="color: var(--text-danger, #ef4444); font-size: 12px; margin-top: 4px; display: none;"></div>
                        </div>
                        
                        <div style="margin-bottom: 20px;">
                            <label style="display: block; color: var(--text-primary); font-size: 14px; font-weight: 500; margin-bottom: 8px;">
                                Description
                            </label>
                            <textarea id="role-description" 
                                      maxlength="500"
                                      rows="3"
                                      style="width: 100%; padding: 10px 12px; background: var(--bg-elevated); border: 1px solid var(--border-muted); border-radius: 6px; color: var(--text-primary); font-size: 14px; resize: vertical; font-family: inherit;"
                                      placeholder="Enter role description"></textarea>
                            <div id="role-description-error" style="color: var(--text-danger, #ef4444); font-size: 12px; margin-top: 4px; display: none;"></div>
                        </div>
                        
                        <div style="margin-bottom: 24px;">
                            <label style="display: block; color: var(--text-primary); font-size: 14px; font-weight: 500; margin-bottom: 12px;">
                                Permissions
                            </label>
                            <div id="role-permissions-container" style="max-height: 400px; overflow-y: auto; border: 1px solid var(--border-muted); border-radius: 6px; padding: 16px; background: var(--bg-elevated);">
                                ${Object.keys(groupedPermissions).length === 0 
                                    ? '<div style="color: var(--text-secondary); font-size: 13px; text-align: center; padding: 20px;">No permissions available</div>'
                                    : Object.keys(groupedPermissions).map(resource => {
                                        const perms = groupedPermissions[resource];
                                        return `
                                            <div style="margin-bottom: 20px;">
                                                <div style="color: var(--text-primary); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border-muted);">
                                                    ${this.escapeHtml(resource)}
                                                </div>
                                                <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px;">
                                                    ${perms.map(perm => `
                                                        <label style="display: flex; align-items: center; padding: 8px; cursor: pointer; border-radius: 4px; transition: background 0.2s;" 
                                                               onmouseover="this.style.background='var(--bg-surface)'" 
                                                               onmouseout="this.style.background='transparent'">
                                                            <input type="checkbox" 
                                                                   value="${perm.id}" 
                                                                   class="role-permission-checkbox"
                                                                   style="margin-right: 10px; cursor: pointer;">
                                                            <div>
                                                                <div style="color: var(--text-primary); font-size: 13px; font-weight: 500;">${this.escapeHtml(perm.action || perm.name || '')}</div>
                                                                ${perm.description ? `<div style="color: var(--text-secondary); font-size: 11px; margin-top: 2px;">${this.escapeHtml(perm.description)}</div>` : ''}
                                                            </div>
                                                        </label>
                                                    `).join('')}
                                                </div>
                                            </div>
                                        `;
                                    }).join('')
                                }
                            </div>
                        </div>
                        
                        <div id="role-editor-error" style="color: var(--text-danger, #ef4444); font-size: 13px; margin-bottom: 16px; display: none;"></div>
                        
                        <div style="display: flex; gap: 12px; justify-content: flex-end;">
                            <button type="button" 
                                    onclick="RoleEditor.close()"
                                    style="padding: 10px 20px; background: transparent; border: 1px solid var(--border-muted); border-radius: 6px; color: var(--text-primary); cursor: pointer; font-size: 14px; font-weight: 500;">
                                Cancel
                            </button>
                            <button type="submit"
                                    style="padding: 10px 20px; background: var(--accent-primary); color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500;">
                                ${isEdit ? 'Update' : 'Create'}
                            </button>
                        </div>
                    </form>
                </div>
            </div>
        `;
        
        // Load role data if editing
        if (isEdit) {
            this.loadRole().then(role => {
                document.getElementById('role-name').value = role.name || '';
                document.getElementById('role-description').value = role.description || '';
                
                // Check permissions
                if (role.permissions && Array.isArray(role.permissions)) {
                    role.permissions.forEach(permId => {
                        const checkbox = document.querySelector(`.role-permission-checkbox[value="${permId}"]`);
                        if (checkbox) {
                            checkbox.checked = true;
                        }
                    });
                }
            }).catch(error => {
                this.showError(error.message);
            });
        }
    },
    
    /**
     * Show modal
     */
    showModal: function() {
        const modal = document.getElementById('role-editor-modal');
        if (modal) {
            modal.style.display = 'block';
        }
    },
    
    /**
     * Close modal
     */
    close: function() {
        const modal = document.getElementById('role-editor-modal');
        if (modal) {
            modal.style.display = 'none';
        }
        this.currentRoleId = null;
    },
    
    /**
     * Save role
     */
    save: function(event) {
        event.preventDefault();
        
        const name = document.getElementById('role-name').value.trim();
        const description = document.getElementById('role-description').value.trim();
        
        // Get selected permissions
        const selectedPermissions = Array.from(document.querySelectorAll('.role-permission-checkbox:checked'))
            .map(cb => cb.value);
        
        // Validation
        if (!name || name.length < 2) {
            this.showFieldError('role-name', 'Role name must be at least 2 characters');
            return;
        }
        
        // Clear errors
        this.clearErrors();
        
        const self = this;
        const url = this.currentRoleId 
            ? `${Auth.getApiUrl()}/api/roles/${this.currentRoleId}`
            : `${Auth.getApiUrl()}/api/roles`;
        const method = this.currentRoleId ? 'PUT' : 'POST';
        
        const data = {
            name: name,
            description: description || null,
            permissions: selectedPermissions
        };
        
        $.ajax({
            url: url,
            method: method,
            contentType: 'application/json',
            headers: {
                'Authorization': `Bearer ${Auth.getToken()}`
            },
            data: JSON.stringify(data),
            success: function(response) {
                if (response.success) {
                    self.showSuccess(this.currentRoleId ? 'Role updated successfully' : 'Role created successfully');
                    self.close();
                    if (typeof RolesTable !== 'undefined') {
                        RolesTable.loadRoles();
                    }
                } else {
                    self.showError(response.error || 'Failed to save role');
                }
            },
            error: function(xhr, status, error) {
                const errorMsg = xhr.responseJSON?.error || error;
                self.showError('Failed to save role: ' + errorMsg);
                
                // Show field-specific errors if available
                if (xhr.responseJSON && xhr.responseJSON.errors) {
                    Object.keys(xhr.responseJSON.errors).forEach(field => {
                        self.showFieldError(`role-${field}`, xhr.responseJSON.errors[field]);
                    });
                }
            }
        });
    },
    
    /**
     * Show field error
     */
    showFieldError: function(fieldId, message) {
        const errorEl = document.getElementById(fieldId + '-error');
        if (errorEl) {
            errorEl.textContent = message;
            errorEl.style.display = 'block';
        }
    },
    
    /**
     * Clear all errors
     */
    clearErrors: function() {
        document.querySelectorAll('[id$="-error"]').forEach(el => {
            el.style.display = 'none';
        });
    },
    
    /**
     * Show error message
     */
    showError: function(message) {
        const errorEl = document.getElementById('role-editor-error');
        if (errorEl) {
            errorEl.textContent = message;
            errorEl.style.display = 'block';
        }
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
     * Escape HTML
     */
    escapeHtml: function(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
};

// Закрытие по клику на overlay — как в других модалках: не закрывать, если выделяли текст
(function() {
    let mouseDownInsideContent = false;
    document.addEventListener('mousedown', function(e) {
        const modal = document.getElementById('role-editor-modal');
        if (!modal || modal.style.display !== 'block') return;
        const content = modal.querySelector(':scope > div');
        mouseDownInsideContent = content ? content.contains(e.target) : (modal.contains(e.target) && e.target !== modal);
    });
    document.addEventListener('click', function(e) {
        const modal = document.getElementById('role-editor-modal');
        if (!modal || modal.style.display !== 'block') return;
        if (e.target === modal && !mouseDownInsideContent) {
            RoleEditor.close();
        }
        mouseDownInsideContent = false;
    });
})();
