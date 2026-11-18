// GPL-3.0-or-later. Adapter for Hash Slinging Slasher's original Meet search.
use slasher::search::Meet;
use std::{collections::HashMap, env, fs::File, io::{self, BufRead, BufReader, Write}};

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let file = File::open(env::args_os().nth(1).ok_or("missing plan")?)?;
    let mut lines = BufReader::new(file).lines();
    let header = lines.next().ok_or("missing header")??;
    let values: Vec<usize> = header.split_whitespace().map(str::parse).collect::<Result<_, _>>()?;
    let [prefixes, suffixes, stems, targets] = values.as_slice() else { return Err("invalid header".into()); };
    let mut read = |count| -> io::Result<Vec<String>> {
        (0..count).map(|_| lines.next().unwrap_or_else(|| Err(io::ErrorKind::UnexpectedEof.into()))).collect()
    };
    let openings = read(*prefixes)?;
    let endings = read(*suffixes)?;
    let words = read(*stems)?;
    let mut wanted = HashMap::with_capacity(*targets);
    for key in read(*targets)? {
        wanted.insert(u64::from_str_radix(&key, 16)?, 0);
    }
    if lines.next().is_some() { return Err("unexpected trailing plan data".into()); }
    let hits = Meet::with_basis(&openings, &endings, slasher::games::IW_BASIS, true).run(&words, &wanted);
    let mut out = io::stdout().lock();
    for (key, name) in hits {
        writeln!(out, "STUDIO_HIT\t{key:016x}\t{name}")?;
    }
    writeln!(out, "STUDIO_DONE")?;
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("CPU suffix plan failed: {error}");
        std::process::exit(1);
    }
}
