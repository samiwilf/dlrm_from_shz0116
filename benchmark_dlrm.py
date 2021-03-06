import subprocess
if __name__ == "__main__":
    
    # Global Batchsize
    MB = [[64,256,1024]]*4

    # Average number of indices for look-up per table
    P = [8,16,16,18]

    # Total number of tables for inference
    S = [308,363,429,527]

    # Dimension of the vector for each row of the table, embedding dimension
    E = [94,127,173,256]

    # Shrink Average number of rows per table to fit in 1 card (use this)
    M = [555693, 347308, 213728, 118233]

    # Length of inputs to bottom MLP
    D = [1414, 1485, 1559, 1715] 

    # Number of bottom MLP layers
    Nb = [8,8,8,10]

    # Bottom MLP size
    Lb = [1750,2100,2500,2750]

    # Number of top MLP layers
    Nt = [36,36,40,45]

    # Top MLP size
    Lt = [1450,1700,2000,2200]

    # Number of vectors resulting from interaction
    I = [8,10,12,14]
    
    use_string_format = False
    for MB,P,S,E,M,D,Nb,Lb,Nt,Lt,I in zip(MB,P,S,E,M,D,Nb,Lb,Nt,Lt,I):
        for MB in MB:
            arch_mlp_bot="-".join([str(D)] + [str(Lb)]*Nb + [str(E)]) 
            arch_mlp_top="-".join([str(Lt)]*Nt + ["1"]) # the number of input features to the first top MLP implicitly determined by interaction 
            arch_embedding_size="-".join([str(M)]*S)  
            cmd = f"python dlrm_s_pytorch.py --arch-sparse-feature-size={E} --arch-mlp-bot=\"{arch_mlp_bot}\" --arch-mlp-top=\"{arch_mlp_top}\" --arch-embedding-size=\"{arch_embedding_size}\" --mini-batch-size={MB} --print-freq=10240 --print-time --num-indices-per-lookup-fixed=1 --num-indices-per-lookup={P} --inference-only --quantize-mlp-with-bit=16 --quantize-emb-with-bit=4 --num-batches=1000"
            cmd = cmd.replace("\"", "")

            print (cmd.split()[0],cmd.split()[1])
            for arg in cmd.split()[2:]:
                print(arg)
            print("")
            stderr_message = None
            process = subprocess.run(cmd.split(), capture_output=True)
            stdout_as_str = process.stdout.decode("utf-8")
            print(stdout_as_str)
            stderr_as_str = process.stderr.decode("utf-8")
            print(stderr_as_str)
            print("")
 