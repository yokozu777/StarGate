import React, { useState } from 'react';

// Mock data
const mockFileTree = {
  name: '12_date_timezone',
  type: 'folder',
  children: [
    {
      name: 'defaults',
      type: 'folder',
      children: [
        { name: 'main.yml', type: 'file', path: 'defaults/main.yml' }
      ]
    },
    {
      name: 'handlers',
      type: 'folder',
      children: [
        { name: 'main.yml', type: 'file', path: 'handlers/main.yml' }
      ]
    },
    {
      name: 'tasks',
      type: 'folder',
      children: [
        { name: 'main.yml', type: 'file', path: 'tasks/main.yml', selected: true }
      ]
    },
    {
      name: 'templates',
      type: 'folder',
      children: [
        { name: 'ntp.conf.j2', type: 'file', path: 'templates/ntp.conf.j2' }
      ]
    }
  ]
};

const mockFileContent = `---
# tasks file for 12_date_timezone
- name: Configure timezone
  timezone:
    name: "{{ timezone | default('UTC') }}"
  
- name: Sync system time
  command: ntpdate -s time.nist.gov
`;

interface FileNode {
  name: string;
  type: 'file' | 'folder';
  path?: string;
  children?: FileNode[];
  selected?: boolean;
  expanded?: boolean;
}

const RawEditor: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'files' | 'runs' | 'lint' | 'variables' | 'meta'>('files');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string>('');
  const [unsavedChanges, setUnsavedChanges] = useState<boolean>(false);
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set(['tasks', 'defaults', 'handlers', 'templates']));

  const roleName = '12_date_timezone';
  const roleStatus = 'ENABLED';
  const roleDescription = 'Configure system date and timezone settings';
  const breadcrumbPath = selectedFile ? `roles/core-roles/${roleName}/${selectedFile}` : '';

  const toggleFolder = (folderName: string) => {
    const newExpanded = new Set(expandedFolders);
    if (newExpanded.has(folderName)) {
      newExpanded.delete(folderName);
    } else {
      newExpanded.add(folderName);
    }
    setExpandedFolders(newExpanded);
  };

  const handleFileSelect = (filePath: string) => {
    setSelectedFile(filePath);
    setFileContent(mockFileContent); // In real app, fetch from API
    setUnsavedChanges(false);
  };

  const handleContentChange = (content: string) => {
    setFileContent(content);
    setUnsavedChanges(true);
  };

  const handleSave = () => {
    // In real app, save to backend
    console.log('Saving file:', selectedFile, fileContent);
    setUnsavedChanges(false);
  };

  const handleDiscard = () => {
    if (confirm('Discard unsaved changes?')) {
      setFileContent(mockFileContent);
      setUnsavedChanges(false);
    }
  };

  const handleBreadcrumbClick = (index: number) => {
    const parts = breadcrumbPath.split('/');
    const pathToNavigate = parts.slice(0, index + 1).join('/');
    
    // Navigate based on breadcrumb level
    if (index === 0) {
      // Clicked on "roles" - navigate to roles list
      window.location.href = '/roles';
    } else if (index === 1) {
      // Clicked on "core-roles" - navigate to roles list (or specific group)
      window.location.href = '/roles';
    } else if (index === 2) {
      // Clicked on role name - navigate to role page
      window.location.href = `/roles/${roleName}`;
    }
    // Last element (file) is not clickable
  };

  const renderFileTree = (nodes: FileNode[], depth: number = 0): React.ReactNode => {
    return nodes.map((node, index) => {
      const isExpanded = node.type === 'folder' && expandedFolders.has(node.name);
      const isSelected = node.path === selectedFile;

      if (node.type === 'folder') {
        return (
          <div key={index}>
            <div
              className={`
                flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer
                hover:bg-gray-700 transition-colors
                ${depth === 0 ? 'font-semibold' : ''}
              `}
              style={{ paddingLeft: `${8 + depth * 20}px` }}
              onClick={() => toggleFolder(node.name)}
            >
              <i className={`fas fa-chevron-${isExpanded ? 'down' : 'right'} text-xs w-3`}></i>
              <i className="fas fa-folder text-amber-400"></i>
              <span className="text-gray-200">{node.name}</span>
            </div>
            {isExpanded && node.children && (
              <div>{renderFileTree(node.children, depth + 1)}</div>
            )}
          </div>
        );
      } else {
        const isYaml = node.name.endsWith('.yml') || node.name.endsWith('.yaml');
        const isJinja = node.name.endsWith('.j2');
        
        return (
          <div
            key={index}
            className={`
              flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer
              transition-colors
              ${isSelected 
                ? 'bg-blue-900/30 text-blue-400' 
                : 'hover:bg-gray-700 text-gray-200'
              }
            `}
            style={{ paddingLeft: `${8 + depth * 20}px` }}
            onClick={() => node.path && handleFileSelect(node.path)}
          >
            <i className="fas w-3"></i>
            <i className={`fas ${
              isYaml ? 'fa-file-code text-green-400' :
              isJinja ? 'fa-file-code text-blue-400' :
              'fa-file text-gray-400'
            }`}></i>
            <span>{node.name}</span>
          </div>
        );
      }
    });
  };

  return (
    <div className="flex flex-col h-full min-h-screen bg-gray-900 text-gray-100">
      {/* Sidebar - Left Navigation */}
      <div className="w-64 bg-gray-800 border-r border-gray-700 p-4">
        <div className="mb-6">
          <h2 className="text-lg font-semibold text-gray-200 mb-2">Workspace</h2>
          <div className="text-sm text-gray-400">Navigation menu</div>
        </div>
      </div>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col">
        {/* Header */}
        <div className="bg-gray-800 border-b border-gray-700 p-6">
          <div className="flex items-start justify-between gap-6">
            <div className="flex-1">
              <div className="flex items-center gap-3 mb-3">
                <h1 className="text-2xl font-bold text-gray-100 flex items-center gap-2">
                  <i className="fas fa-cog text-blue-400"></i>
                  {roleName}
                </h1>
                <span className={`
                  inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold
                  ${roleStatus === 'ENABLED' 
                    ? 'bg-green-900/30 text-green-400 border border-green-700/50' 
                    : 'bg-red-900/30 text-red-400 border border-red-700/50'
                  }
                `}>
                  <i className={`fas ${roleStatus === 'ENABLED' ? 'fa-check-circle' : 'fa-times-circle'} mr-1.5`}></i>
                  {roleStatus}
                </span>
              </div>
              <input
                type="text"
                defaultValue={roleDescription}
                placeholder="Enter role description..."
                className="w-full max-w-2xl px-3 py-2 bg-gray-700 border border-gray-600 rounded text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-blue-500 transition-colors"
              />
            </div>
            <div className="flex gap-2">
              <button className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-gray-200 rounded text-sm font-medium transition-colors flex items-center gap-2">
                <i className="fas fa-check-circle"></i>
                Validate
              </button>
              <button className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-gray-200 rounded text-sm font-medium transition-colors flex items-center gap-2">
                <i className="fas fa-history"></i>
                History
              </button>
              <button className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm font-medium transition-colors flex items-center gap-2">
                <i className="fas fa-play"></i>
                Run
              </button>
            </div>
          </div>
        </div>

        {/* Tabs */}
        <div className="bg-gray-800 border-b border-gray-700">
          <div className="flex gap-1 px-6">
            {[
              { id: 'files', label: 'Files', icon: 'fa-folder-open' },
              { id: 'runs', label: 'Recent runs', icon: 'fa-clock' },
              { id: 'lint', label: 'Lint results', icon: 'fa-exclamation-triangle' },
              { id: 'variables', label: 'Variables', icon: 'fa-list' },
              { id: 'meta', label: 'Meta', icon: 'fa-info-circle' }
            ].map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id as any)}
                className={`
                  px-5 py-3 text-sm font-medium transition-colors border-b-2
                  ${activeTab === tab.id
                    ? 'text-blue-400 border-blue-400'
                    : 'text-gray-400 border-transparent hover:text-gray-200'
                  }
                `}
              >
                <i className={`fas ${tab.icon} mr-2`}></i>
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* Tab Content */}
        <div className="flex-1 flex overflow-hidden">
          {activeTab === 'files' && (
            <>
              {/* Left Panel: File Tree */}
              <div className="w-72 bg-gray-800 border-r border-gray-700 p-4 overflow-y-auto">
                <div className="mb-4 flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-gray-200 flex items-center gap-2">
                    <i className="fas fa-folder-tree"></i>
                    Files
                  </h3>
                </div>
                <div className="space-y-1">
                  {renderFileTree(mockFileTree.children || [])}
                </div>
              </div>

              {/* Right Panel: Code Editor */}
              <div className="flex-1 flex flex-col bg-gray-900">
                {/* Editor Header */}
                {selectedFile && breadcrumbPath && (
                  <div className="bg-gray-800 border-b border-gray-700 px-4 py-3 flex items-center justify-between">
                    <div className="flex items-center gap-2 text-sm text-gray-400">
                      <i className="fas fa-file-code"></i>
                      <span className="flex items-center gap-1">
                        {breadcrumbPath.split('/').filter(part => part.length > 0).map((part, i, arr) => {
                          const isLast = i === arr.length - 1;
                          const isClickable = !isLast;
                          return (
                            <React.Fragment key={i}>
                              {isClickable ? (
                                <button
                                  onClick={() => handleBreadcrumbClick(i)}
                                  className="text-gray-300 hover:text-blue-400 hover:underline transition-colors cursor-pointer"
                                >
                                  {part}
                                </button>
                              ) : (
                                <span className="text-blue-400">{part}</span>
                              )}
                              {!isLast && <i className="fas fa-chevron-right text-xs mx-1 text-gray-500"></i>}
                            </React.Fragment>
                          );
                        })}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      {unsavedChanges && (
                        <span className="text-xs text-amber-400 font-medium flex items-center gap-1">
                          <i className="fas fa-circle text-[6px]"></i>
                          Unsaved changes
                        </span>
                      )}
                      <button
                        onClick={handleSave}
                        className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-gray-200 rounded text-xs font-medium transition-colors flex items-center gap-1"
                      >
                        <i className="fas fa-indent"></i>
                        Format
                      </button>
                      <button
                        onClick={() => console.log('Diff view')}
                        className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-gray-200 rounded text-xs font-medium transition-colors flex items-center gap-1"
                      >
                        <i className="fas fa-code-branch"></i>
                        Diff
                      </button>
                      <button
                        onClick={handleDiscard}
                        className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-gray-200 rounded text-xs font-medium transition-colors flex items-center gap-1"
                      >
                        <i className="fas fa-undo"></i>
                        Discard
                      </button>
                      <button
                        onClick={handleSave}
                        className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded text-xs font-medium transition-colors flex items-center gap-1"
                      >
                        <i className="fas fa-save"></i>
                        Save
                      </button>
                    </div>
                  </div>
                )}

                {/* Code Editor */}
                <div className="flex-1 p-4">
                  <textarea
                    value={fileContent}
                    onChange={(e) => handleContentChange(e.target.value)}
                    disabled={!selectedFile}
                    className="w-full h-full bg-gray-900 text-gray-100 font-mono text-sm p-4 rounded border border-gray-700 focus:outline-none focus:border-blue-500 resize-none disabled:opacity-50 disabled:cursor-not-allowed"
                    style={{ tabSize: 2 }}
                    spellCheck={false}
                    placeholder="Select a file from the tree to edit..."
                  />
                </div>
              </div>
            </>
          )}

          {activeTab === 'runs' && (
            <div className="flex-1 flex items-center justify-center text-gray-400">
              <div className="text-center">
                <i className="fas fa-clock text-5xl mb-4 opacity-50"></i>
                <h3 className="text-xl font-semibold text-gray-200 mb-2">Recent runs</h3>
                <p>Execution history will be displayed here</p>
              </div>
            </div>
          )}

          {activeTab === 'lint' && (
            <div className="flex-1 flex items-center justify-center text-gray-400">
              <div className="text-center">
                <i className="fas fa-exclamation-triangle text-5xl mb-4 opacity-50"></i>
                <h3 className="text-xl font-semibold text-gray-200 mb-2">Lint results</h3>
                <p>Linting results will be displayed here</p>
              </div>
            </div>
          )}

          {activeTab === 'variables' && (
            <div className="flex-1 flex items-center justify-center text-gray-400">
              <div className="text-center">
                <i className="fas fa-list text-5xl mb-4 opacity-50"></i>
                <h3 className="text-xl font-semibold text-gray-200 mb-2">Variables</h3>
                <p>Role variables will be displayed here</p>
              </div>
            </div>
          )}

          {activeTab === 'meta' && (
            <div className="flex-1 flex items-center justify-center text-gray-400">
              <div className="text-center">
                <i className="fas fa-info-circle text-5xl mb-4 opacity-50"></i>
                <h3 className="text-xl font-semibold text-gray-200 mb-2">Meta</h3>
                <p>Role metadata will be displayed here</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default RawEditor;
