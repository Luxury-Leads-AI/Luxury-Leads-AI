(function () {
  const agencyId = document.currentScript.getAttribute("data-agency");
  const BASE_URL = "https://luxury-leads-ai.onrender.com";

  // ── UNIQUE SESSION ID - generated fresh on every page load ──
  // Guarantees each visitor/visit gets an isolated conversation
  const sessionId = 'sess_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 10);

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

    // ---------- FLOAT BUTTON (WhatsApp Style) ----------
    const button = document.createElement("div");
    button.innerHTML = "💬";
    button.id = "luxury-chat-button";
    button.style = `
      position: fixed; bottom: 20px; right: 20px;
      width: 60px; height: 60px;
      background: linear-gradient(135deg, #25D366 0%, #128C7E 100%);
      color: white;
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 28px; cursor: pointer;
      box-shadow: 0 4px 12px rgba(37, 211, 102, 0.4), 0 8px 24px rgba(0,0,0,0.15);
      z-index: 9999;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    `;

    button.onmouseenter = () => {
      button.style.transform = "scale(1.1)";
      button.style.boxShadow = "0 6px 16px rgba(37, 211, 102, 0.5), 0 12px 32px rgba(0,0,0,0.2)";
    };

    button.onmouseleave = () => {
      button.style.transform = "scale(1)";
      button.style.boxShadow = "0 4px 12px rgba(37, 211, 102, 0.4), 0 8px 24px rgba(0,0,0,0.15)";
    };

    document.body.appendChild(button);

    // ---------- PROACTIVE BUBBLE (WhatsApp Style) ----------
    const proactiveBubble = document.createElement("div");
    proactiveBubble.id = "proactive-bubble";
    proactiveBubble.style = `
      position: fixed; bottom: 95px; right: 20px;
      background: #FFFFFF;
      padding: 12px 16px;
      border-radius: 8px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.1), 0 8px 24px rgba(0,0,0,0.08);
      max-width: 260px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: 14px;
      line-height: 1.4;
      color: #303030;
      z-index: 9998;
      display: none;
      cursor: pointer;
      animation: slideInRight 0.3s ease-out;
    `;

    const closeProactive = document.createElement("span");
    closeProactive.innerHTML = "✕";
    closeProactive.style = `
      position: absolute;
      top: 8px;
      right: 10px;
      cursor: pointer;
      color: #8696a0;
      font-size: 18px;
      font-weight: 300;
    `;

    closeProactive.onclick = (e) => {
      e.stopPropagation();
      proactiveBubble.style.display = "none";
      proactiveShown = true;
    };

    proactiveBubble.appendChild(closeProactive);

    const proactiveText = document.createElement("div");
    proactiveText.style = "margin-top: 4px;";
    proactiveBubble.appendChild(proactiveText);

    proactiveBubble.onclick = () => {
      proactiveBubble.style.display = "none";
      chatBox.style.display = "flex";
      chatOpened = true;
      input.focus();
    };

    document.body.appendChild(proactiveBubble);

    // ---------- CHAT BOX (WhatsApp Dark Theme) ----------
    const chatBox = document.createElement("div");
    chatBox.id = "luxury-chat-box";
    chatBox.style = `
      position: fixed; bottom: 90px; right: 20px;
      width: 360px; height: 550px;
      background: #0b141a;
      border-radius: 12px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      box-shadow: 0 2px 8px rgba(0,0,0,0.15), 0 12px 40px rgba(0,0,0,0.25);
      display: none; z-index: 9999;
      overflow: hidden;
      flex-direction: column;
    `;

    chatBox.innerHTML = `
      <div style="
        background: #202c33;
        padding: 14px 16px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        box-shadow: 0 1px 2px rgba(0,0,0,0.1);
      ">
        <div style="display: flex; align-items: center; gap: 12px;">
          <div style="
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: linear-gradient(135deg, #25D366 0%, #128C7E 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
          ">👤</div>
          <div>
            <div style="font-size: 15px; font-weight: 500; color: #e9edef;">${info.assistant}</div>
            <div style="font-size: 12px; color: #8696a0; margin-top: 1px;">from ${info.agency}</div>
          </div>
        </div>
        <span id="chat-close" style="cursor: pointer; font-size: 22px; color: #8696a0; padding: 4px;">✖</span>
      </div>

      <div id="chat-messages" style="
        flex: 1;
        overflow-y: auto;
        padding: 12px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        background: #0b141a;
      "></div>

      <div id="typing-indicator" style="
        padding: 8px 12px;
        display: none;
        background: #0b141a;
      ">
        <div style="
          display: inline-flex;
          gap: 4px;
          padding: 8px 12px;
          background: #202c33;
          border-radius: 8px;
          box-shadow: 0 1px 2px rgba(0,0,0,0.1);
        ">
          <span style="
            width: 8px;
            height: 8px;
            background: #667781;
            border-radius: 50%;
            animation: typing 1.4s infinite;
          "></span>
          <span style="
            width: 8px;
            height: 8px;
            background: #667781;
            border-radius: 50%;
            animation: typing 1.4s infinite 0.2s;
          "></span>
          <span style="
            width: 8px;
            height: 8px;
            background: #667781;
            border-radius: 50%;
            animation: typing 1.4s infinite 0.4s;
          "></span>
        </div>
      </div>

      <div style="background: #202c33; padding: 8px 12px; box-shadow: 0 -1px 2px rgba(0,0,0,0.1);">
        <div style="
          display: flex;
          align-items: center;
          background: #2a3942;
          border-radius: 24px;
          padding: 4px 12px;
          box-shadow: inset 0 1px 2px rgba(0,0,0,0.1);
        ">
          <input id="chat-input" placeholder="Type a message..."
            style="
              flex: 1;
              padding: 10px 8px;
              border: none;
              background: transparent;
              color: #e9edef;
              font-size: 14px;
              outline: none;
              font-family: inherit;
            ">
          <button id="send-button" style="
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: #25D366;
            border: none;
            color: white;
            font-size: 18px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
            box-shadow: 0 2px 4px rgba(37, 211, 102, 0.3);
          ">➤</button>
        </div>
      </div>
    `;

    document.body.appendChild(chatBox);

    const input = chatBox.querySelector("#chat-input");
    const sendButton = chatBox.querySelector("#send-button");
    const messages = chatBox.querySelector("#chat-messages");
    const closeBtn = chatBox.querySelector("#chat-close");
    const typingIndicator = chatBox.querySelector("#typing-indicator");

    sendButton.onmouseenter = () => {
      sendButton.style.transform = "scale(1.05)";
      sendButton.style.boxShadow = "0 4px 8px rgba(37, 211, 102, 0.4)";
    };
    sendButton.onmouseleave = () => {
      sendButton.style.transform = "scale(1)";
      sendButton.style.boxShadow = "0 2px 4px rgba(37, 211, 102, 0.3)";
    };

    const style = document.createElement('style');
    style.textContent = `
      @keyframes slideInRight {
        from { transform: translateX(100px); opacity: 0; }
        to { transform: translateX(0); opacity: 1; }
      }
      @keyframes typing {
        0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
        30% { transform: translateY(-8px); opacity: 1; }
      }
      @keyframes messageIn {
        from { transform: scale(0.9); opacity: 0; }
        to { transform: scale(1); opacity: 1; }
      }
      #chat-messages::-webkit-scrollbar { width: 6px; }
      #chat-messages::-webkit-scrollbar-track { background: #0b141a; }
      #chat-messages::-webkit-scrollbar-thumb { background: #374045; border-radius: 3px; }
      #chat-messages::-webkit-scrollbar-thumb:hover { background: #4a5356; }
    `;
    document.head.appendChild(style);

    // ── NO AUTO-GREETING: chat opens silently, client speaks first ──
    button.onclick = () => {
      chatBox.style.display = "flex";
      chatOpened = true;
      proactiveBubble.style.display = "none";
      input.focus();
    };

    closeBtn.onclick = () => {
      chatBox.style.display = "none";
      chatOpened = false;
    };

    // ---------- MESSAGE BUBBLE (WhatsApp Style) ----------
    function addBubble(text, sender) {
      const wrapper = document.createElement("div");
      wrapper.style = `
        display: flex;
        flex-direction: column;
        align-items: ${sender === "user" ? "flex-end" : "flex-start"};
        animation: messageIn 0.2s ease-out;
        margin-bottom: 2px;
      `;

      const bubble = document.createElement("div");
      bubble.innerText = text;
      bubble.style = `
        padding: 8px 12px;
        border-radius: ${sender === "user" ? "8px 8px 0px 8px" : "8px 8px 8px 0px"};
        max-width: 75%;
        font-size: 14px;
        line-height: 1.5;
        background: ${sender === "user" ? "#005c4b" : "#202c33"};
        color: #e9edef;
        word-wrap: break-word;
        box-shadow: 0 1px 2px rgba(0,0,0,0.15);
        position: relative;
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

      typingIndicator.style.display = "block";
      messages.scrollTop = messages.scrollHeight;

      try {
        const response = await fetch(`${BASE_URL}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: text,
            agency_id: agencyId,
            session_id: sessionId   // ← unique per page load
          })
        });

        let data;
        try {
          data = await response.json();
        } catch {
          data = { reply: "Connection issue. Please try again!" };
        }

        typingIndicator.style.display = "none";

        setTimeout(() => {
          addBubble(data.reply || "Sorry, could you rephrase?", "ai");
        }, 500);

      } catch (error) {
        typingIndicator.style.display = "none";
        addBubble("Connection error. Please check internet.", "ai");
      }
    }

    input.addEventListener("keypress", function (e) {
      if (e.key === "Enter") {
        const text = input.value.trim();
        sendMessage(text);
      }
    });

    sendButton.onclick = () => {
      const text = input.value.trim();
      sendMessage(text);
    };

    // ---------- BEHAVIORAL TRIGGERS (unchanged) ----------

    setTimeout(() => {
      if (!chatOpened && !proactiveShown) {
        proactiveText.innerHTML = `<strong>👋 Hi there!</strong><br>Need help finding a property?`;
        proactiveBubble.style.display = "block";
        proactiveShown = true;
      }
    }, 5000);

    let exitIntentShown = false;
    document.addEventListener('mouseleave', (e) => {
      if (e.clientY < 10 && !chatOpened && !exitIntentShown && messagesExchanged === 0) {
        proactiveBubble.style.display = "none";
        proactiveText.innerHTML = `<strong>⏰ Before you go!</strong><br>Get notified when properties match your needs.`;
        proactiveBubble.style.display = "block";
        exitIntentShown = true;
        proactiveShown = true;
      }
    });

    const hasVisitedBefore = localStorage.getItem('luxury_leads_visited');
    if (hasVisitedBefore && !chatOpened) {
      setTimeout(() => {
        if (!proactiveShown) {
          proactiveText.innerHTML = `<strong>👋 Welcome back!</strong><br>Still looking? Let me help.`;
          proactiveBubble.style.display = "block";
          proactiveShown = true;
        }
      }, 3000);
    }
    localStorage.setItem('luxury_leads_visited', 'true');

    if (window.location.href.includes('/property') ||
        window.location.href.includes('/listing') ||
        window.location.href.includes('/homes')) {
      setTimeout(() => {
        if (!chatOpened && !proactiveShown) {
          proactiveText.innerHTML = `<strong>🏡 Interested in this property?</strong><br>I have details on schools & neighborhood!`;
          proactiveBubble.style.display = "block";
          proactiveShown = true;
        }
      }, 12000);
    }

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

    setTimeout(() => {
      if (!chatOpened && messagesExchanged === 0) {
        proactiveBubble.style.display = "none";
        proactiveText.innerHTML = `<strong>💡 Quick question:</strong><br>What type of property are you looking for?`;
        proactiveBubble.style.display = "block";
        proactiveShown = true;
      }
    }, 60000);

  });
})();