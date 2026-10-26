// Derived from the official Google batchEmbedContents contract; synthetic data only.
const http = require('node:http');
http.createServer(async (req, res) => {
  if (req.method !== 'POST' || !(req.url === '/models/gemini-embedding-001:batchEmbedContents' || /^\/models\/[^/]+:generateContent$/.test(req.url))) {res.writeHead(501);res.end();return;}
  const bytes=[];for await(const chunk of req)bytes.push(chunk);
  const input=JSON.parse(Buffer.concat(bytes).toString('utf8'));

  if(req.url.endsWith(':generateContent')){
    const prompt=input.contents[0].parts[0].text;
    const planned=prompt.includes('Slots:\n');
    const data=JSON.parse(prompt.split(planned?'Slots:\n':'Reference JSON follows:\n')[1]);
    const slots=planned?data:[{excerpts:data.excerpts,correct_option_count:1}];
    const output={questions:slots.map((slot,index)=>{
      const source=slot.excerpts[0];
      const invalid=(slot.objective||'').includes('fixture-invalid-answers');
      const correct=invalid?1:slot.correct_option_count;
      const citations=[{chunk_id:source.chunk_id,quote:source.text.slice(0,100)}];
      if((slot.objective||'').includes('fixture-duplicate-citations')) citations.push({...citations[0]});
      return {question_text:`Question ${index+1}: which statements describe a stack?`,options:['LIFO','Last in first out','FIFO','Sorted'].map((text,i)=>({text,isCorrect:i<correct})),citations};
    })};
    res.writeHead(200,{'Content-Type':'application/json'});
    res.end(JSON.stringify({candidates:[{content:{parts:[{text:JSON.stringify(output)}],role:'model'},finishReason:'STOP'}]}));return;
  }
  const text=JSON.stringify(input);
  const status=text.includes('fixture-quota')?429:text.includes('fixture-config')?403:200;
  res.writeHead(status,{'Content-Type':'application/json'});
  if(status!==200){res.end(JSON.stringify({error:{code:status,message:'synthetic failure'}}));return;}
  res.end(JSON.stringify({embeddings:input.requests.map(()=>({values:Array.from({length:768},(_,i)=>i===0?1:0)}))}));
}).listen(process.env.PORT || 4962);
