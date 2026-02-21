(function () {
  const agencyId = document.currentScript.getAttribute("data-agency");
  const BASE_URL = "https://luxury-leads-ai.onrender.com";

  let chatOpened = false;
  let proactiveShown = false;
  let messagesExchanged = 0;

  async function getAgencyInfo() {
    try {
      const res = await fetch(`${BASE_URL}/agency/${agencyId}`);
      const data = await res.json();
      return {
        agency: data.name || "Assistant",
        assistant: data.assistant || "Assistant"
      };
    } catch {
      return { agency: "Assistant", assistant: "Assistant" };
    }
  }

  getAgencyInfo().then((info) => {

    // ---------- FLOAT BUTTON ----------
    const button = document.createElement("div");
    button.innerHTML = "💬";
    button.id = "luxury-chat-button";
    button.style = `
      position: fixed; bottom: 20px; right: 20px;
      width: 60px; height: 60px;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      color: white;
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 28px; cursor: pointer;
      box-shadow: 0 5px 20px rgba(102, 126, 234, 0.4);
      z-index: 9999;
      transition: transform 0.3s, box-shadow 0.3s;
    `;
    
    button.onmouseenter = () => {
      button.style.transform = "scale(1.1)";
      button.style.boxShadow = "0 8px 25px rgba(102, 126, 234, 0.6)";
    };
    
    button.onmouseleave = () => {
      button.style.transform = "scale(1)";
      button.style.boxShadow = "0 5px 20px rgba(102, 126, 234, 0.4)";
    };
    
    document.body.appendChild(button);

    // ---------- PROACTIVE MESSAGE BUBBLE ----------
    const proactiveBubble = document.createElement("div");
    proactiveBubble.id = "proactive-bubble";
    proactiveBubble.style = `
      position: fixed; bottom: 95px; right: 20px;
      background: white;
      padding: 14px 18px;
      border-radius: 18px;
      box-shadow: 0 4px 15px rgba(0,0,0,0.15);
      max-width: 260px;
      font-family: Arial, sans-serif;
      font-size: 14px;
      line-height: 1.4;
      color: #333;
      z-index: 9998;
      display: none;
      cursor: pointer;
      animation: slideInRight 0.4s ease-out;
    `;
    
    const closeProactive = document.createElement("span");
    closeProactive.innerHTML = "✕";
    closeProactive.style = `
      position: absolute;
      top: 6px;
      right: 10px;
      cursor: pointer;
      color: #999;
      font-size: 16px;
    `;
    
    closeProactive.onclick = (e) => {
      e.stopPropagation();
      proactiveBubble.style.display = "none";
      proactiveShown = true;
    };
    
    proactiveBubble.appendChild(closeProactive);
    
    const proactiveText = document.createElement("div");
    proactiveText.style = "margin-top: 8px;";
    proactiveBubble.appendChild(proactiveText);
    
    proactiveBubble.onclick = () => {
      proactiveBubble.style.display = "none";
      chatBox.style.display = "flex";
      chatOpened = true;
      input.focus();
    };
    
    document.body.appendChild(proactiveBubble);

    // ---------- CHAT BOX ----------
    const chatBox = document.createElement("div");
    chatBox.id = "luxury-chat-box";
    chatBox.style = `
      position: fixed; bottom: 90px; right: 20px;
      width: 360px; height: 520px;
      background: #121212; color: white;
      border-radius: 16px;
      font-family: Arial, sans-serif;
      box-shadow: 0 8px 30px rgba(0,0,0,0.6);
      display: none; z-index: 9999;
      overflow: hidden;
      flex-direction: column;
    `;

    chatBox.innerHTML = `
      <div style="
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 16px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-weight: bold;
      ">
        <div>
          <div style="font-size: 16px;">${info.assistant}</div>
          <div style="font-size: 11px; opacity: 0.9; margin-top: 2px;">from ${info.agency}</div>
        </div>
        <span id="chat-close" style="cursor: pointer; font-size: 20px;">✖</span>
      </div>

      <div id="chat-messages" style="
        flex: 1;
        overflow-y: auto;
        padding: 14px;
        display: flex;
        flex-direction: column;
        gap: 10px;
        background: #1a1a1a;
      "></div>

      <div id="typing-indicator" style="
        padding: 8px 14px;
        display: none;
        background: #1a1a1a;
      ">
        <div style="
          display: inline-flex;
          gap: 4px;
          padding: 8px 12px;
          background: #2a2a2a;
          border-radius: 18px;
        ">
          <span style="
            width: 8px;
            height: 8px;
            background: #666;
            border-radius: 50%;
            animation: typing 1.4s infinite;
          "></span>
          <span style="
            width: 8px;
            height: 8px;
            background: #666;
            border-radius: 50%;
            animation: typing 1.4s infinite 0.2s;
          "></span>
          <span style="
            width: 8px;
            height: 8px;
            background: #666;
            border-radius: 50%;
            animation: typing 1.4s infinite 0.4s;
          "></span>
        </div>
      </div>

      <div style="background: #1a1a1a; padding: 12px;">
        <input id="chat-input" placeholder="Type your message..."
          style="
            width: 100%;
            padding: 12px;
            border: none;
            border-radius: 24px;
            background: #2a2a2a;
            color: white;
            font-size: 14px;
            outline: none;
          ">
      </div>
    `;

    document.body.appendChild(chatBox);

    const input = chatBox.querySelector("#chat-input");
    const messages = chatBox.querySelector("#chat-messages");
    const closeBtn = chatBox.querySelector("#chat-close");
    const typingIndicator = chatBox.querySelector("#typing-indicator");

    // Add typing animation CSS
    const style = document.createElement('style');
    style.textContent = `
      @keyframes slideInRight {
        from {
          transform: translateX(100px);
          opacity: 0;
        }
        to {
          transform: translateX(0);
          opacity: 1;
        }
      }
      
      @keyframes typing {
        0%, 60%, 100% {
          transform: translateY(0);
          opacity: 0.5;
        }
        30% {
          transform: translateY(-10px);
          opacity: 1;
        }
      }
      
      #chat-messages::-webkit-scrollbar {
        width: 6px;
      }
      
      #chat-messages::-webkit-scrollbar-track {
        background: #1a1a1a;
      }
      
      #chat-messages::-webkit-scrollbar-thumb {
        background: #444;
        border-radius: 3px;
      }
    `;
    document.head.appendChild(style);

    button.onclick = () => {
      chatBox.style.display = "flex";
      chatOpened = true;
      proactiveBubble.style.display = "none";
      input.focus();
      
      // Send initial greeting if first time
      if (messagesExchanged === 0) {
        setTimeout(() => {
          addBubble(`Hey! 👋 I'm ${info.assistant}. What brings you here today?`, "ai");
        }, 800);
      }
    };
    
    closeBtn.onclick = () => {
      chatBox.style.display = "none";
      chatOpened = false;
    };

    // ---------- MESSAGE BUBBLE ----------
    function addBubble(text, sender) {
      const wrapper = document.createElement("div");
      wrapper.style = `
        display: flex;
        flex-direction: column;
        align-items: ${sender === "user" ? "flex-end" : "flex-start"};
        animation: slideInRight 0.3s ease-out;
      `;

      const bubble = document.createElement("div");
      bubble.innerText = text;
      bubble.style = `
        padding: 10px 14px;
        border-radius: 18px;
        max-width: 75%;
        font-size: 14px;
        line-height: 1.5;
        background: ${sender === "user" ? "linear-gradient(135deg, #667eea 0%, #764ba2 100%)" : "#2a2a2a"};
        color: white;
        word-wrap: break-word;
        box-shadow: 0 2px 8px rgba(0,0,0,0.2);
      `;

      wrapper.appendChild(bubble);
      messages.appendChild(wrapper);
      messages.scrollTop = messages.scrollHeight;
    }

    // ---------- SEND MESSAGE ----------
    async function sendMessage(text) {
      if (!text || !text.trim()) return;

      addBubble(text, "user");
      input.value = "";
      messagesExchanged++;

      // Show typing indicator
      typingIndicator.style.display = "block";
      messages.scrollTop = messages.scrollHeight;

      try {
        const response = await fetch(`${BASE_URL}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text, agency_id: agencyId })
        });

        let data;
        try {
          data = await response.json();
        } catch {
          data = { reply: "I'm having trouble connecting. Please try again!" };
        }

        // Hide typing indicator
        typingIndicator.style.display = "none";

        // Simulate human typing delay
        setTimeout(() => {
          addBubble(data.reply || "Sorry, I didn't catch that. Could you rephrase?", "ai");
        }, 600);

      } catch (error) {
        typingIndicator.style.display = "none";
        addBubble("Connection issue. Please check your internet and try again.", "ai");
      }
    }

    input.addEventListener("keypress", function (e) {
      if (e.key === "Enter") {
        const text = input.value.trim();
        sendMessage(text);
      }
    });

    // ---------- BEHAVIORAL TRIGGERS ----------

    // TRIGGER 1: Warm Greeter (5 seconds on page)
    setTimeout(() => {
      if (!chatOpened && !proactiveShown) {
        proactiveText.innerHTML = `<strong>👋 Hi there!</strong><br>Need help finding a property? I'm here to assist!`;
        proactiveBubble.style.display = "block";
        proactiveShown = true;
      }
    }, 5000);

    // TRIGGER 2: Exit Intent Detection
    let exitIntentShown = false;
    document.addEventListener('mouseleave', (e) => {
      if (e.clientY < 10 && !chatOpened && !exitIntentShown && messagesExchanged === 0) {
        proactiveBubble.style.display = "none";
        proactiveText.innerHTML = `<strong>⏰ Before you go!</strong><br>Want me to email you when properties matching your needs become available?`;
        proactiveBubble.style.display = "block";
        exitIntentShown = true;
        proactiveShown = true;
      }
    });

    // TRIGGER 3: Return Visitor (check localStorage)
    const hasVisitedBefore = localStorage.getItem('luxury_leads_visited');
    if (hasVisitedBefore && !chatOpened) {
      setTimeout(() => {
        if (!proactiveShown) {
          proactiveText.innerHTML = `<strong>👋 Welcome back!</strong><br>Still looking? I can help you find what you need.`;
          proactiveBubble.style.display = "block";
          proactiveShown = true;
        }
      }, 3000);
    }
    localStorage.setItem('luxury_leads_visited', 'true');

    // TRIGGER 4: Page-Specific Engagement
    // Detect if on property listing page (customize URL pattern as needed)
    if (window.location.href.includes('/property') || 
        window.location.href.includes('/listing') ||
        window.location.href.includes('/homes')) {
      
      setTimeout(() => {
        if (!chatOpened && !proactiveShown) {
          proactiveText.innerHTML = `<strong>🏡 Interested in this property?</strong><br>I have details on schools, neighborhood, and tour availability!`;
          proactiveBubble.style.display = "block";
          proactiveShown = true;
        }
      }, 12000); // 12 seconds on listing page
    }

    // TRIGGER 5: Scroll Engagement (50% down page)
    let scrollTriggered = false;
    window.addEventListener('scroll', () => {
      const scrollPercent = (window.scrollY / (document.body.scrollHeight - window.innerHeight)) * 100;
      
      if (scrollPercent > 50 && !chatOpened && !scrollTriggered && !proactiveShown) {
        proactiveText.innerHTML = `<strong>🔍 Finding what you need?</strong><br>I can help narrow down your search!`;
        proactiveBubble.style.display = "block";
        scrollTriggered = true;
        proactiveShown = true;
      }
    });

    // TRIGGER 6: Time on Page (60 seconds = serious interest)
    setTimeout(() => {
      if (!chatOpened && messagesExchanged === 0) {
        proactiveBubble.style.display = "none";
        proactiveText.innerHTML = `<strong>💡 Quick question:</strong><br>What type of property are you looking for? I can show you our best matches.`;
        proactiveBubble.style.display = "block";
        proactiveShown = true;
      }
    }, 60000);

  });
})();