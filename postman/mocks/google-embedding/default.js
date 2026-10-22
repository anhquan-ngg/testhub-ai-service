// Derived from the official Google batchEmbedContents contract; synthetic data only.
const http = require('node:http');
http.createServer(async (req, res) => {
  if (req.method !== 'POST' || !(req.url === '/models/gemini-embedding-001:batchEmbedContents' || /^\/models\/[^/]+:generateContent$/.test(req.url))) {res.writeHead(501);res.end();return;}
  const bytes=[];for await(const chunk of req)bytes.push(chunk);
  const input=JSON.parse(Buffer.concat(bytes).toString('utf8'));

  if(req.url.endsWith(':generateContent')){
    const prompt=input.contents[0].parts[0].text;
    const data=JSON.parse(prompt.split('Reference JSON follows:\n')[1]);
    const source=data.excerpts[0];
    const output={questions:[{question_text:'Which ordering principle does a stack use?',options:['LIFO','FIFO','Random','Sorted'].map((text,i)=>({text,isCorrect:i===0})),citations:[{chunk_id:source.chunk_id,quote:source.text.slice(0,100)}]}]};
    res.writeHead(200,{'Content-Type':'application/json'});
    res.end(JSON.stringify({candidates:[{content:{parts:[{text:JSON.stringify(output)}],role:'model'},finishReason:'STOP'}]}));return;
  }
  const text=JSON.stringify(input);
  const status=text.includes('fixture-quota')?429:text.includes('fixture-config')?403:200;
  res.writeHead(status,{'Content-Type':'application/json'});
  if(status!==200){res.end(JSON.stringify({error:{code:status,message:'synthetic failure'}}));return;}
  res.end(JSON.stringify({embeddings:input.requests.map(()=>({values:Array.from({length:768},(_,i)=>i===0?1:0)}))}));
}).listen(process.env.PORT || 4962);
