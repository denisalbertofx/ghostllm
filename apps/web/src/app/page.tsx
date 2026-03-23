"use client"

import React, { useState, useEffect, useRef } from 'react'
import { 
  MessageSquare, 
  Settings, 
  Terminal, 
  BarChart3, 
  Cpu, 
  Send, 
  ShieldCheck, 
  LogOut,
  RefreshCw,
  Activity,
  Layers,
  ChevronRight,
  Database,
  Search
} from 'lucide-react'

// Mock User Data (shold be from Auth in final, but we hardcode for the local demo)
const USER = { id: 1, username: 'admin', role: 'admin', apiKey: 'fg-SP32l6ibdFIXEQyk1Nov1KqQYhKHxM0jM1T2amKu--s' }

export default function GhostDashboard() {
  const [activeTab, setActiveTab] = useState<'chat' | 'models' | 'logs' | 'stats'>('chat')
  const [messages, setMessages] = useState<{role: string, content: string}[]>([])
  const [input, setInput] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [selectedModel, setSelectedModel] = useState('kimi')
  const [models, setModels] = useState<any[]>([])
  const [stats, setStats] = useState({ tokens: 0, cost: 0.0 })
  const scrollRef = useRef<HTMLDivElement>(null)

  // Fetch models on init
  useEffect(() => {
    fetch('/v1/models').then(res => res.json()).then(data => {
      if (data.data) setModels(data.data)
    }).catch(e => console.error("Failed to load models", e))
  }, [])

  // Auto-scroll chat
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages])

  const handleSend = async () => {
    if (!input.trim() || isStreaming) return

    const newMessages = [...messages, { role: 'user', content: input }]
    setMessages(newMessages)
    setInput('')
    setIsStreaming(true)

    // Placeholder for assistant
    setMessages([...newMessages, { role: 'assistant', content: '' }])

    try {
      const response = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': USER.apiKey
        },
        body: JSON.stringify({
          model: selectedModel,
          messages: newMessages,
          stream: true
        })
      })

      if (!response.ok) throw new Error("API Error")

      const reader = response.body?.getReader()
      const decoder = new TextDecoder()
      let assistantText = ''

      while (true) {
        const { done, value } = await reader!.read()
        if (done) break

        const chunk = decoder.decode(value)
        const lines = chunk.split('\n')
        
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const dataStr = line.slice(6)
            if (dataStr === '[DONE]') continue
            try {
              const data = JSON.parse(dataStr)
              const delta = data.choices[0].delta.content || ''
              assistantText += delta
              setMessages([...newMessages, { role: 'assistant', content: assistantText }])
            } catch (e) {}
          }
        }
      }
    } catch (e) {
      console.error(e)
      setMessages([...newMessages, { role: 'assistant', content: '⚠️ Error connecting to GhostLLM daemon.' }])
    } finally {
      setIsStreaming(false)
    }
  }

  return (
    <div className="flex h-screen bg-[#050505] text-white overflow-hidden font-sans selection:bg-blue-500/30">
      {/* Sidebar - Premium Minimalist */}
      <aside className="w-80 bg-[#0a0a0a] border-r border-white/5 flex flex-col p-8 z-20">
        <div className="flex items-center gap-4 mb-16 group cursor-default">
          <div className="w-10 h-10 bg-gradient-to-br from-blue-600 to-indigo-700 rounded-2xl flex items-center justify-center shadow-lg shadow-blue-500/20 group-hover:scale-105 transition-transform duration-300">
            <Layers className="w-6 h-6 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-bold tracking-tight">GhostLLM</h1>
            <p className="text-[10px] text-white/30 tracking-[0.2em] font-semibold uppercase">Forge Dashboard</p>
          </div>
        </div>

        <div className="space-y-1">
          <p className="px-4 text-[10px] font-bold text-white/20 uppercase tracking-widest mb-4">Core Services</p>
          <NavItem active={activeTab === 'chat'} onClick={() => setActiveTab('chat')} icon={<MessageSquare />} label="Chat Forge" />
          <NavItem active={activeTab === 'models'} onClick={() => setActiveTab('models')} icon={<Database />} label="Model Registry" />
          <NavItem active={activeTab === 'logs'} onClick={() => setActiveTab('logs')} icon={<Terminal />} label="Live Logs" />
          <NavItem active={activeTab === 'stats'} onClick={() => setActiveTab('stats')} icon={<BarChart3 />} label="Analytics" />
        </div>

        <div className="mt-auto space-y-6">
           <div className="p-5 bg-white/[0.02] border border-white/5 rounded-3xl backdrop-blur-md">
              <div className="flex items-center justify-between mb-4">
                 <span className="text-[10px] font-bold text-white/30 uppercase">System Status</span>
                 <div className="flex items-center gap-2">
                    <span className="w-1.5 h-1.5 bg-green-500 rounded-full animate-pulse" />
                    <span className="text-[10px] text-green-500 font-bold uppercase">Healthy</span>
                 </div>
              </div>
              <div className="space-y-4">
                 <div>
                    <div className="flex justify-between text-[10px] mb-2 text-white/40">
                       <span>NVIDIA NIM LATENCY</span>
                       <span className="text-white/60">24ms</span>
                    </div>
                    <div className="h-1 bg-white/5 rounded-full overflow-hidden">
                       <div className="h-full bg-blue-600 w-1/4 rounded-full" />
                    </div>
                 </div>
                 <div>
                    <div className="flex justify-between text-[10px] mb-2 text-white/40">
                       <span>LOCAL QUOTA (FREE)</span>
                       <span className="text-white/60">82%</span>
                    </div>
                    <div className="h-1 bg-white/5 rounded-full overflow-hidden">
                       <div className="h-full bg-indigo-600 w-[82%] rounded-full shadow-[0_0_8px_rgba(79,70,229,0.5)]" />
                    </div>
                 </div>
              </div>
           </div>

           <div className="flex items-center gap-4 px-2">
              <div className="w-10 h-10 rounded-full bg-gradient-to-tr from-gray-800 to-gray-700 flex items-center justify-center font-bold text-sm border border-white/10">
                 {USER.username[0].toUpperCase()}
              </div>
              <div className="flex-1">
                 <p className="text-sm font-semibold">{USER.username}</p>
                 <p className="text-[10px] text-white/30 uppercase font-bold tracking-wider">{USER.role}</p>
              </div>
              <button className="p-2 text-white/20 hover:text-red-400 transition-colors">
                 <LogOut className="w-4 h-4" />
              </button>
           </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <main className="flex-1 flex flex-col min-w-0 bg-[#050505]">
        {/* Header */}
        <header className="h-24 border-b border-white/5 flex items-center justify-between px-10 bg-[#050505]/80 backdrop-blur-2xl sticky top-0 z-10">
          <div className="flex items-center gap-8">
            <div className="flex items-center gap-3">
              <span className="w-2 h-2 bg-blue-500 rounded-full" />
              <h2 className="text-sm font-bold tracking-tight text-white/80">
                {activeTab === 'chat' && 'LLM Inference Workspace'}
                {activeTab === 'models' && 'Registry & Allowlist'}
                {activeTab === 'logs' && 'System Observation'}
                {activeTab === 'stats' && 'Consumption Metrics'}
              </h2>
            </div>
            
            {activeTab === 'chat' && (
              <div className="h-10 bg-white/5 rounded-2xl flex items-center px-4 border border-white/5 group hover:border-white/10 transition-colors">
                <Search className="w-4 h-4 text-white/20 group-hover:text-white/40 transition-colors" />
                <input 
                  type="text" 
                  placeholder="Search for models..." 
                  className="bg-transparent border-none outline-none text-xs px-3 w-48 placeholder:text-white/20"
                />
              </div>
            )}
          </div>

          <div className="flex items-center gap-4">
            <div className="flex bg-white/5 p-1 rounded-2xl border border-white/5">
               <button className="px-4 py-2 rounded-xl text-xs font-bold bg-[#151515] shadow-lg">Cloud</button>
               <button className="px-4 py-2 rounded-xl text-xs font-bold text-white/20 hover:text-white/40 transition-colors">Local</button>
            </div>
            <div className="w-px h-6 bg-white/10 mx-2" />
            <button className="w-10 h-10 rounded-2xl border border-white/5 flex items-center justify-center hover:bg-white/5 transition-colors">
               <Settings className="w-4 h-4 text-white/40" />
            </button>
          </div>
        </header>

        {/* Dynamic Screen */}
        <div className="flex-1 flex flex-col overflow-hidden relative">
          
          {/* Chat Interface */}
          {activeTab === 'chat' && (
            <div className="flex-1 flex flex-col h-full">
              <div ref={scrollRef} className="flex-1 overflow-y-auto px-10 py-10 space-y-8 custom-scrollbar">
                {messages.length === 0 ? (
                  <div className="h-full flex flex-col items-center justify-center text-center max-w-md mx-auto">
                    <div className="w-20 h-20 bg-blue-600/10 rounded-[2.5rem] flex items-center justify-center mb-8 border border-blue-600/20">
                      <MessageSquare className="w-10 h-10 text-blue-500" />
                    </div>
                    <h3 className="text-2xl font-bold mb-3">GhostLLM Forge</h3>
                    <p className="text-sm text-white/40 leading-relaxed mb-10">
                      Start an optimized inference session using NVIDIA NIM. Your requests are routed securely via the local Forge gateway.
                    </p>
                    <div className="grid grid-cols-2 gap-4 w-full">
                       <SuggestionCard text="Summarize the Forge logs" />
                       <SuggestionCard text="Analyze Kimi k2.5 limits" />
                    </div>
                  </div>
                ) : (
                  messages.map((m, i) => (
                    <div key={i} className={`flex w-full ${m.role === 'user' ? 'justify-end' : 'justify-start slide-up'}`}>
                      <div className={`max-w-[80%] overflow-hidden ${
                        m.role === 'user' ? 'bg-blue-600 text-white rounded-3xl rounded-tr-none px-6 py-4 shadow-xl shadow-blue-600/20' 
                        : 'bg-[#121212] border border-white/5 rounded-3xl rounded-tl-none px-7 py-5'
                      }`}>
                        <div className="flex items-center gap-3 mb-3 border-b border-white/5 pb-2">
                           <span className="text-[10px] font-bold tracking-widest uppercase opacity-40">{m.role}</span>
                           {m.role === 'assistant' && (
                             <span className="text-[10px] font-bold tracking-widest uppercase text-blue-500">KIMI K2.5</span>
                           )}
                        </div>
                        <p className="text-[15px] leading-[1.8] font-light tracking-wide whitespace-pre-wrap">
                          {m.content || (
                            <span className="flex items-center gap-2 text-white/30 italic italic">
                               <RefreshCw className="w-3 h-3 animate-spin" /> Forge is thinking...
                            </span>
                          )}
                        </p>
                      </div>
                    </div>
                  ))
                )}
              </div>

              {/* Input Zone */}
              <div className="p-10 pt-0">
                 <div className="max-w-5xl mx-auto relative group">
                    <div className="absolute -inset-1 bg-gradient-to-r from-blue-600 to-indigo-600 rounded-[2.5rem] blur opacity-10 group-focus-within:opacity-20 transition duration-1000"></div>
                    <div className="relative bg-[#101010] border border-white/5 rounded-[2.5rem] shadow-2xl overflow-hidden focus-within:border-blue-500/50 transition-all">
                       <div className="px-8 py-4 flex items-center justify-between border-b border-white/5 bg-white/[0.01]">
                          <div className="flex items-center gap-6">
                             <select 
                                value={selectedModel}
                                onChange={(e) => setSelectedModel(e.target.value)}
                                className="bg-transparent text-[10px] font-bold text-white/40 uppercase tracking-widest cursor-pointer hover:text-white transition-colors outline-none"
                             >
                               {models.length > 0 ? models.map(m => (
                                 <option key={m.id} value={m.name || m.id} className="bg-[#101010]">{m.name || m.id}</option>
                               )) : <option value="kimi">kimi k2.5</option>}
                             </select>
                             <div className="flex items-center gap-2">
                                <span className="w-1.5 h-1.5 bg-blue-500 rounded-full shadow-[0_0_8px_rgba(59,130,246,0.8)]" />
                                <span className="text-[10px] font-bold text-blue-500 uppercase">Reasoning Active</span>
                             </div>
                          </div>
                          <div className="flex items-center gap-3">
                             <span className="text-[10px] font-bold text-white/20 uppercase">Tokens: {stats.tokens}</span>
                          </div>
                       </div>
                       <div className="flex items-end px-8 py-6">
                         <textarea 
                            rows={1}
                            value={input}
                            onChange={(e) => setInput(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter' && !e.shiftKey) {
                                e.preventDefault()
                                handleSend()
                              }
                            }}
                            placeholder="Type your prompt here..."
                            className="flex-1 bg-transparent border-none outline-none resize-none text-base placeholder:text-white/10 font-light pr-10"
                            style={{ height: 'auto', maxHeight: '200px' }}
                         />
                         <button 
                            disabled={!input.trim() || isStreaming}
                            onClick={handleSend}
                            className="bg-blue-600 hover:bg-blue-500 disabled:opacity-30 disabled:grayscale p-4 rounded-2xl shadow-xl shadow-blue-600/30 transition-all hover:scale-105 active:scale-95"
                         >
                            {isStreaming ? <RefreshCw className="w-5 h-5 animate-spin" /> : <Send className="w-5 h-5" />}
                         </button>
                       </div>
                    </div>
                 </div>
              </div>
            </div>
          )}

          {/* Other tabs as placeholders for now */}
          {activeTab !== 'chat' && (
            <div className="h-full flex flex-col items-center justify-center text-center p-20 opacity-20">
               <Cpu className="w-24 h-24 mb-6" />
               <h3 className="text-xl font-bold uppercase tracking-widest">{activeTab} interface</h3>
               <p className="mt-2">Module registered. Initializing view...</p>
            </div>
          )}
        </div>
      </main>
      
      <style jsx global>{`
        .slide-up {
          animation: slideUp 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes slideUp {
          from { opacity: 0; transform: translateY(20px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .custom-scrollbar::-webkit-scrollbar { width: 5px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.05); border-radius: 10px; }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.1); }
      `}</style>
    </div>
  )
}

function NavItem({ active, icon, label, onClick }: any) {
  return (
    <button 
      onClick={onClick}
      className={`group flex items-center justify-between w-full px-5 py-4 rounded-2xl transition-all duration-300 ${
        active 
        ? 'bg-blue-600/10 text-blue-500' 
        : 'hover:bg-white/[0.03] text-white/30 hover:text-white'
      }`}
    >
      <div className="flex items-center gap-5">
        <div className={`transition-colors duration-300 ${active ? 'text-blue-500' : 'text-white/20 group-hover:text-white/60'}`}>
          {React.cloneElement(icon, { size: 18, strokeWidth: active ? 2.5 : 2 })}
        </div>
        <span className={`text-[13px] font-semibold tracking-wide ${active ? 'opacity-100' : 'opacity-70 group-hover:opacity-100'}`}>{label}</span>
      </div>
      {active && <ChevronRight className="w-4 h-4" />}
    </button>
  )
}

function SuggestionCard({ text }: { text: string }) {
  return (
    <button className="p-4 bg-white/[0.02] border border-white/5 rounded-2xl text-[11px] font-medium text-white/30 text-left hover:border-white/20 hover:text-white/60 transition-all">
       {text}
    </button>
  )
}
