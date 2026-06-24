// INT-12: diagnostic — moved from inline to external (RC-07: removes unsafe-inline CSP)
(function(){
  function show(msg){var d=document.getElementById('_diag');if(d){d.className='_diag-visible';d.textContent=msg;}}
  window.onerror=function(){show('\u9875\u9762\u52a0\u8f7d\u5f02\u5e38\uff0c\u8bf7\u5f3a\u5236\u5237\u65b0(Ctrl+Shift+R)');};
  window.addEventListener('unhandledrejection',function(){show('\u9875\u9762\u52a0\u8f7d\u5f02\u5e38\uff0c\u8bf7\u5f3a\u5236\u5237\u65b0(Ctrl+Shift+R)');});
  setTimeout(function(){
    var d=document.getElementById('_diag');
    if(d&&!document.getElementById('root').children.length){
      show('\u9875\u9762\u672a\u52a0\u8f7d\uff0c\u6b63\u5728\u68c0\u6d4b\u670d\u52a1...');
      fetch('/api/health').then(function(r){return r.json();}).then(function(){show('\u670d\u52a1\u6b63\u5e38\uff0c\u4f46\u9875\u9762\u672a\u52a0\u8f7d\uff0c\u8bf7\u5f3a\u5236\u5237\u65b0(Ctrl+Shift+R)');}).catch(function(){show('\u670d\u52a1\u4e0d\u53ef\u8fbe\uff0c\u8bf7\u68c0\u67e5\u7f51\u7edc\u540e\u5237\u65b0');});
    }
  },5000);
})();
